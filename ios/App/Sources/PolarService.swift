// Pipeline dell'app (polarlab/core/pipeline.py): Bluetooth → decoder → motore → registrazione.
//
// Tutto gira su una sola coda seriale, la stessa dei delegate CoreBluetooth: il calcolo
// costa ~0.1% di CPU, quindi non serve un secondo thread (e si evitano risvegli inutili).
// L'interfaccia legge uno `Snapshot` con `queue.sync` solo quando è visibile.

import Foundation
import PolarKit

final class PolarService {
    let queue = DispatchQueue(label: "polarlab.pipeline", qos: .userInitiated)
    private(set) var link: H10Link!
    private var decoder = StreamDecoder()
    private var engine = Engine()
    private var writer: SessionWriter?
    private var device: [String: Any] = [:]
    private var demoTimer: DispatchSourceTimer?
    private var watchdog: DispatchSourceTimer?
    private var lastPacket = Date()
    /// Dopo questo silenzio la registrazione si chiude da sola (fascia tolta e dimenticata).
    static let autoStopAfter: TimeInterval = 600

    /// Chiamate sul main thread.
    var onState: ((LinkState) -> Void)?
    var onDevice: ((_ name: String, _ info: [String: String], _ streams: [String: String]) -> Void)?
    var onAutoStopped: ((URL) -> Void)?

    init() {
        link = H10Link(queue: queue)
        link.onPacket = { [weak self] p in self?.handle(p) }
        link.onState = { [weak self] s in DispatchQueue.main.async { self?.onState?(s) } }
        link.onInfo = { [weak self] name, id, info, streams in
            guard let self else { return }
            self.device = ["source": "Polar H10", "device": name, "address": id, "device_info": info, "streams": streams]
            DispatchQueue.main.async { self.onDevice?(name, info, streams) }
        }
    }

    private func handle(_ p: Packet) {
        lastPacket = Date()
        writer?.write(p)
        for ev in decoder.feed(p) { engine.handle(ev) }
    }

    // MARK: collegamento

    func connect(accEnabled: Bool) {
        queue.async {
            if self.writer == nil { self.resetAnalysis() }
            self.link.accEnabled = accEnabled
            self.link.start()
        }
    }

    func disconnect() {
        queue.async {
            self.stopDemoLocked()
            self.link.stop()
        }
    }

    func forgetStrap() {
        queue.async { self.link.forget() }
    }

    private func resetAnalysis() {
        decoder = StreamDecoder()
        let notch = engine.notchEnabled
        engine = Engine()
        engine.notchEnabled = notch
    }

    var notchEnabled: Bool {
        get { queue.sync { engine.notchEnabled } }
        set { queue.async { self.engine.notchEnabled = newValue } }
    }

    func snapshot(ecgSeconds: Double) -> Snapshot {
        queue.sync { engine.snapshot(ecgSeconds: ecgSeconds) }
    }

    // MARK: registrazione

    func startRecording(dir: URL, resume: Bool = false) throws {
        try queue.sync {
            guard writer == nil else { return }
            let w = try SessionWriter(dir: dir, meta: device, resume: resume)
            // Apre il log con la configurazione corrente: il replay sa così come decodificare
            for p in decoder.configPackets(hostT: Date().timeIntervalSince1970) { w.write(p) }
            writer = w
            lastPacket = Date()
            startWatchdog()
        }
    }

    /// Chiude raw.plab; l'esportazione delle CSV la fa poi `SessionStore`.
    func stopRecording() -> URL? {
        queue.sync { closeWriterLocked() }
    }

    private func closeWriterLocked() -> URL? {
        watchdog?.cancel()
        watchdog = nil
        guard let w = writer else { return nil }
        writer = nil
        return try? w.close()
    }

    /// Un controllo ogni minuto, con ampia tolleranza: iOS può accorparlo ad altri risvegli.
    private func startWatchdog() {
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + 60, repeating: 60, leeway: .seconds(20))
        t.setEventHandler { [weak self] in
            guard let self, self.writer != nil, Date().timeIntervalSince(self.lastPacket) > Self.autoStopAfter,
                  let dir = self.closeWriterLocked() else { return }
            DispatchQueue.main.async { self.onAutoStopped?(dir) }
        }
        t.resume()
        watchdog = t
    }

    /// Salva su disco i pacchetti in memoria (prima che iOS sospenda l'app).
    func flush() {
        queue.async { self.writer?.flush() }
    }

    var isRecording: Bool { queue.sync { writer != nil } }

    // MARK: demo (simulatore Xcode o senza fascia)

    /// Riproduce in tempo reale una sessione registrata con la H10 vera, in ciclo.
    func startDemo() {
        queue.async { [self] in
            guard self.demoTimer == nil, let url = Bundle.main.url(forResource: "demo", withExtension: "plab"),
                  let packets = try? readPackets(url), let first = packets.first else { return }
            self.link.stop()
            self.resetAnalysis()
            self.device = ["source": "Demo", "device": "Demo (registrazione H10)", "streams": ["ECG": "130 Hz", "ACC": "100 Hz"]]
            DispatchQueue.main.async {
                self.onState?(.streaming)
                self.onDevice?("Demo", [:], ["ECG": "130 Hz", "ACC": "100 Hz"])
            }
            var i = 0
            var start = Date().timeIntervalSince1970
            let timer = DispatchSource.makeTimerSource(queue: self.queue)
            timer.schedule(deadline: .now(), repeating: 0.1, leeway: .milliseconds(50))
            timer.setEventHandler { [weak self] in
                guard let self else { return }
                let now = Date().timeIntervalSince1970
                while i < packets.count, packets[i].hostT - first.hostT <= now - start {
                    var p = packets[i]
                    p.hostT = start + (p.hostT - first.hostT)
                    self.handle(p)
                    i += 1
                }
                if i >= packets.count {
                    i = 0
                    start = now
                }
            }
            timer.resume()
            self.demoTimer = timer
        }
    }

    private func stopDemoLocked() {
        demoTimer?.cancel()
        demoTimer = nil
    }

    var isDemo: Bool { queue.sync { demoTimer != nil } }
}
