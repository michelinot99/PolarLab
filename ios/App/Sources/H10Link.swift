// Collegamento con la Polar H10 via CoreBluetooth: stessa sequenza di polarlab/sources/polar_ble.py.
//
//   1. fascia memorizzata (UUID) → connect diretto; altrimenti scansione del servizio 0x180D
//   2. Device Information, batteria, notifiche HR / batteria / PMD Control Point / PMD Data
//   3. lettura del Control Point → funzioni; per ECG e ACC: GET_SETTINGS → START
//   4. disconnessione → connect pendente: iOS ricollega da solo appena la fascia torna
//      raggiungibile, senza scansioni ripetute (niente consumo extra)
//
// Ogni byte scambiato esce come `Packet`, esattamente come su Mac: la registrazione è identica.

import CoreBluetooth
import Foundation
import PolarKit

enum GATT {
    static let hrService = CBUUID(string: "180D")
    static let hrMeasurement = CBUUID(string: "2A37")
    static let batteryService = CBUUID(string: "180F")
    static let batteryLevel = CBUUID(string: "2A19")
    static let deviceInfo = CBUUID(string: "180A")
    static let infoFields: [CBUUID: String] = [
        CBUUID(string: "2A29"): "manufacturer", CBUUID(string: "2A24"): "model", CBUUID(string: "2A25"): "serial",
        CBUUID(string: "2A27"): "hardware", CBUUID(string: "2A26"): "firmware", CBUUID(string: "2A28"): "software",
    ]
    static let pmdService = CBUUID(string: "FB005C80-02E7-F387-1CAD-8ACD2D8DF0C8")
    static let pmdControl = CBUUID(string: "FB005C81-02E7-F387-1CAD-8ACD2D8DF0C8")
    static let pmdData = CBUUID(string: "FB005C82-02E7-F387-1CAD-8ACD2D8DF0C8")
}

enum LinkState: Equatable {
    case unavailable(String)    // Bluetooth spento o permesso negato
    case idle
    case scanning
    case connecting
    case configuring
    case streaming
    case reconnecting

    var label: String {
        switch self {
        case .unavailable(let why): why
        case .idle: "Non collegata"
        case .scanning: "Cerco la fascia…"
        case .connecting: "Collegamento…"
        case .configuring: "Avvio ECG…"
        case .streaming: "Collegata"
        case .reconnecting: "Fascia persa, ricollego…"
        }
    }
}

final class H10Link: NSObject {
    static let savedIdKey = "polarlab.strapId"
    static let restoreId = "polarlab.central"

    private let queue: DispatchQueue
    private var central: CBCentralManager!
    private var peripheral: CBPeripheral?
    private var wanted = false
    var accEnabled = true

    var onPacket: ((Packet) -> Void)?
    var onState: ((LinkState) -> Void)?
    var onInfo: ((_ name: String, _ id: String, _ info: [String: String], _ streams: [String: String]) -> Void)?

    private var state: LinkState = .idle {
        didSet { if state != oldValue { onState?(state) } }
    }

    // Stato della configurazione PMD
    private var chars: [CBUUID: CBCharacteristic] = [:]
    private var info: [String: String] = [:]
    private var streams: [String: String] = [:]
    private var steps: [Step] = []
    private var expecting: (op: UInt8, mt: UInt8)?
    private var timeout: DispatchWorkItem?
    private var notifying: Set<CBUUID> = []
    private var started: [MeasurementType] = []
    private var servicesHandled = false

    private enum Step {
        case readFeatures
        case getSettings(MeasurementType)
        case start(MeasurementType, [SettingType: Int])
        case stop(MeasurementType)
    }

    init(queue: DispatchQueue) {
        self.queue = queue
        super.init()
        var options: [String: Any] = [CBCentralManagerOptionShowPowerAlertKey: true]
        #if os(iOS)
        options[CBCentralManagerOptionRestoreIdentifierKey] = Self.restoreId
        #endif
        central = CBCentralManager(delegate: self, queue: queue, options: options)
    }

    static var savedId: UUID? {
        get { UserDefaults.standard.string(forKey: savedIdKey).flatMap(UUID.init(uuidString:)) }
        set { UserDefaults.standard.set(newValue?.uuidString, forKey: savedIdKey) }
    }

    // MARK: comandi (sempre sulla coda `queue`)

    func start() {
        wanted = true
        if central.state == .poweredOn { connectOrScan() }
    }

    /// Ferma gli stream e chiude il collegamento: la fascia smette di trasmettere.
    func stop() {
        wanted = false
        timeout?.cancel()
        if central.isScanning { central.stopScan() }
        guard let p = peripheral else {
            state = .idle
            return
        }
        if p.state == .connected, let cp = chars[GATT.pmdControl] {
            for mt in started { write(cp, Pmd.buildStop(mt), on: p) }
        }
        started = []
        queue.asyncAfter(deadline: .now() + 0.3) { [weak self] in
            guard let self, !self.wanted else { return }
            self.central.cancelPeripheralConnection(p)
            self.state = .idle
        }
    }

    func forget() {
        stop()
        Self.savedId = nil
        peripheral = nil
    }

    private func connectOrScan() {
        guard wanted else { return }
        if let p = peripheral, p.state == .connected {
            configure(p)
            return
        }
        if let id = Self.savedId, let p = central.retrievePeripherals(withIdentifiers: [id]).first {
            connect(p)
            return
        }
        // Una fascia già collegata al telefono (es. da un'altra app) non fa advertising
        if let p = central.retrieveConnectedPeripherals(withServices: [GATT.hrService])
            .first(where: { ($0.name ?? "").hasPrefix("Polar H10") }) {
            connect(p)
            return
        }
        state = .scanning
        central.scanForPeripherals(withServices: [GATT.hrService])
    }

    private func connect(_ p: CBPeripheral) {
        peripheral = p
        p.delegate = self
        state = state == .reconnecting ? .reconnecting : .connecting
        // Una connect pendente su iOS non scade mai: è la riconnessione automatica
        central.connect(p)
    }

    // MARK: configurazione

    private func configure(_ p: CBPeripheral) {
        state = .configuring
        chars = [:]
        info = [:]
        streams = [:]
        notifying = []
        started = []
        steps = []
        expecting = nil
        servicesHandled = false
        p.discoverServices([GATT.hrService, GATT.batteryService, GATT.deviceInfo, GATT.pmdService])
    }

    private func servicesReady(_ p: CBPeripheral) {
        for (uuid, c) in chars {
            if GATT.infoFields[uuid] != nil { p.readValue(for: c) }
        }
        if let b = chars[GATT.batteryLevel] {
            p.readValue(for: b)
            p.setNotifyValue(true, for: b)
        }
        if let hr = chars[GATT.hrMeasurement] { p.setNotifyValue(true, for: hr) }
        if let cp = chars[GATT.pmdControl], let data = chars[GATT.pmdData] {
            p.setNotifyValue(true, for: cp)
            p.setNotifyValue(true, for: data)
        } else {
            streams["ECG"] = "servizio PMD assente: solo HR/RR"
            state = .streaming
            publishInfo(p)
        }
    }

    private func notificationsReady(_ p: CBPeripheral) {
        guard notifying.contains(GATT.pmdControl), notifying.contains(GATT.pmdData), steps.isEmpty, expecting == nil,
              state == .configuring else { return }
        steps = [.readFeatures]
        runNextStep(p)
    }

    private func runNextStep(_ p: CBPeripheral) {
        guard let cp = chars[GATT.pmdControl] else { return }
        guard !steps.isEmpty else {
            state = .streaming
            publishInfo(p)
            return
        }
        let step = steps[0]
        switch step {
        case .readFeatures:
            expecting = (Pmd.featuresId, 0)
            p.readValue(for: cp)
        case .getSettings(let mt):
            expect(ControlOp.getSettings.rawValue, mt)
            write(cp, Pmd.buildGetSettings(mt), on: p)
        case .start(let mt, let s):
            expect(ControlOp.start.rawValue, mt)
            write(cp, Pmd.buildStart(mt, s), on: p)
        case .stop(let mt):
            expect(ControlOp.stop.rawValue, mt)
            write(cp, Pmd.buildStop(mt), on: p)
        }
        armTimeout(p)
    }

    private func expect(_ op: UInt8, _ mt: MeasurementType) { expecting = (op, mt.rawValue) }

    private func armTimeout(_ p: CBPeripheral) {
        timeout?.cancel()
        let w = DispatchWorkItem { [weak self] in
            // Nessuna risposta dalla fascia: si chiude e si riparte da capo
            guard let self, self.expecting != nil else { return }
            self.expecting = nil
            self.steps = []
            self.central.cancelPeripheralConnection(p)
        }
        timeout = w
        queue.asyncAfter(deadline: .now() + 5, execute: w)
    }

    private func write(_ c: CBCharacteristic, _ cmd: [UInt8], on p: CBPeripheral) {
        onPacket?(Packet(hostT: Date().timeIntervalSince1970, channel: .pmdCPWrite, data: cmd))
        p.writeValue(Data(cmd), for: c, type: .withResponse)
    }

    private func wantedSettings(_ mt: MeasurementType) -> [SettingType: Int] {
        mt == .ecg ? [.sampleRate: 130, .resolution: 14] : [.sampleRate: 100, .resolution: 16, .range: 8]
    }

    private func handleControl(_ data: [UInt8], _ p: CBPeripheral) {
        guard let exp = expecting, !steps.isEmpty else { return }
        if exp.op == Pmd.featuresId {
            guard data.first == Pmd.featuresId, let features = try? Pmd.parseFeatures(data) else { return }
            done()
            for mt in [MeasurementType.ecg, .acc] where mt == .ecg || accEnabled {
                if features.contains(mt) { steps.append(.getSettings(mt)) } else { streams[mt.name] = "non supportato" }
            }
            runNextStep(p)
            return
        }
        guard data.first == Pmd.controlResponseId, let r = try? Pmd.parseControlResponse(data),
              r.op == exp.op, r.measType == exp.mt, let mt = MeasurementType(rawValue: r.measType) else { return }
        let step = steps[0]
        done()
        switch step {
        case .getSettings:
            if r.ok, let avail = try? r.settings() {
                let chosen = Pmd.chooseSettings(avail, wantedSettings(mt))
                streams[mt.name] = Pmd.describe(chosen.mapValues { [Double($0)] })
                steps.insert(.start(mt, chosen), at: 0)
            }
        case .start(_, let s):
            if r.error == .alreadyInState {
                // Stream rimasto attivo da una sessione precedente: fermalo e riparti pulito
                steps.insert(contentsOf: [.stop(mt), .start(mt, s)], at: 0)
            } else if r.ok {
                started.append(mt)
            } else {
                streams[mt.name] = "rifiutato: \(r.error)"
            }
        case .stop, .readFeatures:
            break
        }
        runNextStep(p)
    }

    private func done() {
        timeout?.cancel()
        expecting = nil
        steps.removeFirst()
    }

    private func publishInfo(_ p: CBPeripheral) {
        var i = info
        i["mtu"] = String(p.maximumWriteValueLength(for: .withoutResponse) + 3)
        onInfo?(p.name ?? "Polar H10", p.identifier.uuidString, i, streams)
    }
}

extension H10Link: CBCentralManagerDelegate {
    func centralManagerDidUpdateState(_ c: CBCentralManager) {
        switch c.state {
        case .poweredOn:
            if state.isUnavailable { state = .idle }
            if wanted { connectOrScan() }
        case .poweredOff: state = .unavailable("Bluetooth spento")
        case .unauthorized: state = .unavailable("Permesso Bluetooth negato: Impostazioni → PolarLab")
        case .unsupported: state = .unavailable("Bluetooth non disponibile su questo dispositivo")
        default: break
        }
    }

    #if os(iOS)
    func centralManager(_ c: CBCentralManager, willRestoreState dict: [String: Any]) {
        // iOS ha rilanciato l'app in background durante una corsa: si riprende la fascia
        if let p = (dict[CBCentralManagerRestoredStatePeripheralsKey] as? [CBPeripheral])?.first {
            peripheral = p
            p.delegate = self
            wanted = true
        }
    }
    #endif

    func centralManager(_ c: CBCentralManager, didDiscover p: CBPeripheral, advertisementData: [String: Any], rssi: NSNumber) {
        let name = p.name ?? (advertisementData[CBAdvertisementDataLocalNameKey] as? String) ?? ""
        guard name.hasPrefix("Polar H10") else { return }
        c.stopScan()
        Self.savedId = p.identifier
        connect(p)
    }

    func centralManager(_ c: CBCentralManager, didConnect p: CBPeripheral) {
        configure(p)
    }

    func centralManager(_ c: CBCentralManager, didFailToConnect p: CBPeripheral, error: Error?) {
        if wanted { connect(p) }
    }

    func centralManager(_ c: CBCentralManager, didDisconnectPeripheral p: CBPeripheral, error: Error?) {
        timeout?.cancel()
        expecting = nil
        steps = []
        started = []
        if wanted {
            state = .reconnecting
            central.connect(p)
        } else {
            state = .idle
        }
    }
}

extension H10Link: CBPeripheralDelegate {
    func peripheral(_ p: CBPeripheral, didDiscoverServices error: Error?) {
        for s in p.services ?? [] { p.discoverCharacteristics(nil, for: s) }
    }

    func peripheral(_ p: CBPeripheral, didDiscoverCharacteristicsFor s: CBService, error: Error?) {
        for c in s.characteristics ?? [] { chars[c.uuid] = c }
        if !servicesHandled, (p.services ?? []).allSatisfy({ $0.characteristics != nil }) {
            servicesHandled = true
            servicesReady(p)
        }
    }

    func peripheral(_ p: CBPeripheral, didUpdateNotificationStateFor c: CBCharacteristic, error: Error?) {
        if c.isNotifying { notifying.insert(c.uuid) }
        notificationsReady(p)
    }

    func peripheral(_ p: CBPeripheral, didUpdateValueFor c: CBCharacteristic, error: Error?) {
        guard error == nil, let value = c.value else { return }
        let data = [UInt8](value)
        let now = Date().timeIntervalSince1970
        switch c.uuid {
        case GATT.pmdData:
            onPacket?(Packet(hostT: now, channel: .pmdData, data: data))
        case GATT.hrMeasurement:
            onPacket?(Packet(hostT: now, channel: .hr, data: data))
        case GATT.batteryLevel:
            onPacket?(Packet(hostT: now, channel: .battery, data: data))
        case GATT.pmdControl:
            onPacket?(Packet(hostT: now, channel: .pmdCP, data: data))
            handleControl(data, p)
        default:
            if let key = GATT.infoFields[c.uuid] {
                info[key] = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .controlCharacters.union(.whitespaces))
            }
        }
    }
}

private extension LinkState {
    var isUnavailable: Bool {
        if case .unavailable = self { return true }
        return false
    }
}
