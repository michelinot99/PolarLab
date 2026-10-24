// Stato dell'interfaccia e regole per la batteria.
//
// • Con lo schermo spento o l'app in background non si aggiorna nulla: niente timer,
//   niente disegno. Il calcolo continua solo se si sta registrando.
// • Se non si registra e l'app va in background, la fascia viene scollegata: smette di
//   trasmettere (risparmia anche la sua batteria) e si ricollega da sola al ritorno.
// • ECG a 20 fotogrammi/s solo se visibile; le metriche si aggiornano una volta al secondo.
//
// Salvataggio: con «registrazione automatica» ogni collegamento della fascia diventa una
// sessione; se iOS chiude l'app durante la corsa, al rilancio si riprende la stessa
// sessione; dopo 10 minuti senza dati la registrazione si chiude da sola.

import Foundation
import Observation
import PolarKit
#if os(iOS)
import UIKit
#endif

@MainActor
@Observable
final class LiveModel {
    let service = PolarService()
    let store = SessionStore()

    var state: LinkState = .idle
    var strapName = ""
    var strapInfo: [String: String] = [:]
    var streams: [String: String] = [:]
    var metrics = Metrics()
    var ecg = Snapshot()
    var recordingDir: URL?
    var recordingSince: Date?
    var lastError: String?

    var showECG: Bool = UserDefaults.standard.object(forKey: "showECG") as? Bool ?? true {
        didSet { UserDefaults.standard.set(showECG, forKey: "showECG"); restartTimer() }
    }
    var accEnabled: Bool = UserDefaults.standard.object(forKey: "accEnabled") as? Bool ?? true {
        didSet { UserDefaults.standard.set(accEnabled, forKey: "accEnabled") }
    }
    var notchEnabled = false {
        didSet { service.notchEnabled = notchEnabled }
    }
    var keepScreenOn: Bool = UserDefaults.standard.bool(forKey: "keepScreenOn") {
        didSet { UserDefaults.standard.set(keepScreenOn, forKey: "keepScreenOn"); applyIdleTimer() }
    }
    var autoRecord: Bool = UserDefaults.standard.object(forKey: "autoRecord") as? Bool ?? true {
        didSet { UserDefaults.standard.set(autoRecord, forKey: "autoRecord") }
    }
    private static let activeKey = "activeRecording"

    /// L'utente vuole la fascia collegata (si ricollega al ritorno dal background).
    private var wantConnected = false
    private var demo = false
    private var active = true
    private var timer: Timer?
    private var tickCount = 0

    init() {
        service.onState = { [weak self] s in self?.stateChanged(s) }
        service.onDevice = { [weak self] name, info, streams in
            self?.strapName = name
            self?.strapInfo = info
            self?.streams = streams
        }
        service.onAutoStopped = { [weak self] dir in self?.recordingEnded(dir) }
        // Corsa interrotta da iOS (app terminata in background): si continua la stessa sessione
        var resumed: URL?
        if let path = UserDefaults.standard.string(forKey: Self.activeKey),
           FileManager.default.fileExists(atPath: path) {
            let dir = URL(fileURLWithPath: path, isDirectory: true)
            if (try? service.startRecording(dir: dir, resume: true)) != nil {
                resumed = dir
                recordingDir = dir
                recordingSince = Date()
            }
        }
        store.recoverUnfinished(except: resumed)
        // Se c'è già una fascia memorizzata ci si collega subito
        if H10Link.savedId != nil || resumed != nil { connect() }
    }

    private func stateChanged(_ s: LinkState) {
        state = s
        if s == .streaming && autoRecord && !demo && !isRecording { toggleRecording() }
    }

    var isRecording: Bool { recordingDir != nil }
    var isDemo: Bool { demo }
    var isStreaming: Bool { state == .streaming }

    // MARK: azioni

    func connect() {
        wantConnected = true
        demo = false
        service.connect(accEnabled: accEnabled)
    }

    func disconnect() {
        wantConnected = false
        demo = false
        if isRecording { toggleRecording() }
        service.disconnect()
        metrics = Metrics()
        ecg = Snapshot()
    }

    func startDemo() {
        wantConnected = false
        demo = true
        service.startDemo()
    }

    func forgetStrap() {
        disconnect()
        service.forgetStrap()
        strapName = ""
        strapInfo = [:]
    }

    func toggleRecording() {
        if let dir = recordingDir {
            _ = service.stopRecording()
            recordingEnded(dir)
        } else {
            let dir = store.newSessionDir()
            do {
                try service.startRecording(dir: dir)
                recordingDir = dir
                recordingSince = Date()
                UserDefaults.standard.set(dir.path, forKey: Self.activeKey)
                store.reload()
            } catch {
                lastError = "Impossibile creare la sessione: \(error.localizedDescription)"
            }
        }
        applyIdleTimer()
    }

    private func recordingEnded(_ dir: URL) {
        recordingDir = nil
        recordingSince = nil
        UserDefaults.standard.removeObject(forKey: Self.activeKey)
        store.process(dir)
        applyIdleTimer()
    }

    // MARK: ciclo di vita

    func becameActive() {
        active = true
        if demo && !service.isDemo { service.startDemo() }
        if wantConnected && !isStreaming { service.connect(accEnabled: accEnabled) }
        store.reload()
        restartTimer()
        applyIdleTimer()
    }

    func wentBackground() {
        active = false
        timer?.invalidate()
        timer = nil
        service.flush()
        if !isRecording {
            // Nessuna corsa in corso: si chiude il Bluetooth, lo si riapre al ritorno
            if demo { service.disconnect() } else if wantConnected { service.disconnect() }
        }
    }

    private func restartTimer() {
        timer?.invalidate()
        guard active else { return }
        let interval = showECG ? 1.0 / 20.0 : 1.0
        let t = Timer(timeInterval: interval, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.refresh() }
        }
        t.tolerance = interval * 0.3
        RunLoop.main.add(t, forMode: .common)
        timer = t
        refresh()
    }

    private func refresh() {
        let snap = service.snapshot(ecgSeconds: 5)
        if showECG { ecg = snap }
        tickCount += 1
        // Le metriche cambiano una volta al secondo: si evita di ridisegnare il resto a 20 Hz
        if !showECG || tickCount % 20 == 0 { metrics = snap.metrics }
    }

    private func applyIdleTimer() {
        #if os(iOS)
        UIApplication.shared.isIdleTimerDisabled = keepScreenOn && isRecording
        #endif
    }
}
