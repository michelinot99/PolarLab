// Motore di analisi: consuma gli eventi del decoder e mantiene stato e metriche
// (polarlab/core/engine.py). Non è thread-safe: va usato da una sola coda seriale.

import Foundation

let maxPaddedGapS = 1.0     // buchi più brevi si riempiono, più lunghi azzerano il rilevatore
let ecgHistoryS = 30.0
let accHistoryS = 12.0
let rrHistoryS = 900.0

public struct Metrics {
    public var hrEcg = Double.nan
    public var hrStrap = Double.nan
    public var meanNN = Double.nan
    public var rmssd = Double.nan
    public var sdnn = Double.nan
    public var pnn50 = Double.nan
    public var sd1 = Double.nan
    public var sd2 = Double.nan
    public var lf = Double.nan
    public var hf = Double.nan
    public var lfHf = Double.nan
    public var dfaA1 = Double.nan
    public var dfaBeats = 0
    public var artifactPct = Double.nan
    public var quality = Double.nan
    public var qualityLabel = "—"
    public var templateCorr = Double.nan
    public var strapMaeMs = Double.nan
    public var strapMatchPct = Double.nan
    public var respBrpm = Double.nan
    public var respSource = "—"
    public var respConf = 0.0
    public var cadenceSpm = Double.nan
    public var motionMg = Double.nan
    public var verticalOscCm = Double.nan
    public var contact: Bool?
    public var battery: Int?
    public var beats = 0

    public init() {}

    /// Colonne di metrics.csv, nello stesso ordine di PolarLab per Mac.
    public static let csvHeader = ["t", "hr_ecg", "hr_strap", "mean_nn", "rmssd", "sdnn", "pnn50", "sd1", "sd2", "lf", "hf",
                                   "lf_hf", "dfa_a1", "dfa_beats", "artifact_pct", "quality", "quality_label", "template_corr",
                                   "strap_mae_ms", "strap_match_pct", "resp_brpm", "resp_source", "resp_conf", "cadence_spm",
                                   "motion_mg", "vertical_osc_cm", "battery", "beats"]

    func csvRow(t: Double) -> [String] {
        func f(_ v: Double) -> String { v.isFinite ? String(format: "%.3f", v) : "" }
        return [f(t), f(hrEcg), f(hrStrap), f(meanNN), f(rmssd), f(sdnn), f(pnn50), f(sd1), f(sd2), f(lf), f(hf), f(lfHf),
                f(dfaA1), String(dfaBeats), f(artifactPct), f(quality), qualityLabel, f(templateCorr), f(strapMaeMs),
                f(strapMatchPct), f(respBrpm), respSource, f(respConf), f(cadenceSpm), f(motionMg), f(verticalOscCm),
                battery.map(String.init) ?? "", String(beats)]
    }
}

public struct BeatLogRow {
    public var t, rrMs, nnMs: Double
    public var kind: BeatKind
}

/// Copia leggera per l'interfaccia: ultimi secondi di ECG e metriche correnti.
public struct Snapshot {
    public var metrics = Metrics()
    public var ecg: [Double] = []           // µV filtrati, ultimi `ecgSeconds`
    public var ecgFs = 130.0
    public var ecgEnd = 0                   // indice assoluto del campione successivo all'ultimo
    public var beatIdx: [Double] = []       // indici assoluti dei picchi R nella finestra
    public var rrNN: [Double] = []          // ultimi intervalli NN (ms)
    public var now = 0.0                    // s dall'inizio dello stream

    public init() {}

    init(metrics: Metrics, ecgFs: Double, ecgEnd: Int) {
        self.metrics = metrics
        self.ecgFs = ecgFs
        self.ecgEnd = ecgEnd
    }
}

public final class Engine {
    public private(set) var metrics = Metrics()
    public private(set) var t0: Double?
    public var keepLog: Bool
    public private(set) var beatLog: [BeatLogRow] = []
    public private(set) var metricsLog: [[String]] = []
    /// Callback per ogni riga di metriche (1 Hz), usata per scrivere metrics.csv durante la corsa.
    public var onTick: ((Metrics, Double) -> Void)?

    public private(set) var ecgFs = 130.0
    public private(set) var accFs = 200.0
    private var ecgT: Ring<Double>!, ecgRaw: Ring<Double>!, ecgBp: Ring<Double>!, ecgDisp: Ring<Double>!
    private var filter: EcgFilter!
    private var qrs: QRSDetector!
    private var template: BeatTemplate!
    private var cleaner = RRCleaner()
    private var pendingTemplate: [Beat] = []
    private var beats: [(idx: Double, amp: Double)] = []
    private var lastBeatT: Double?
    private var lastBeatIdx = -1.0
    private var gapIndex = -1

    private var accT: Ring<Double>!, acc: Ring<SIMD3<Double>>!, vert: Ring<Double>!
    private var motion: MotionAnalyzer!
    private var accRs: UniformResampler!
    private var decim = 1
    private var decimBuf: [SIMD3<Double>] = []

    private var strapRR: [(t: Double, rr: Double)] = []
    private var rr: [(t: Double, raw: Double, nn: Double, ok: Bool)] = []
    private let hrvWin = NNWindow(windowS: 60)
    private let hrvLong = NNWindow(windowS: 300)
    private let dfaWin = NNWindow(windowS: 120)
    private let quality = QualityMonitor()
    private let resp = RespirationEstimator()
    private var nextTick: Double?
    private var tickCount = 0

    public init(keepLog: Bool = false) {
        self.keepLog = keepLog
        setupEcg(130)
        setupAcc(200)
    }

    private func setupEcg(_ fs: Double) {
        ecgFs = fs
        let cap = Int(ecgHistoryS * fs)
        ecgT = Ring(capacity: cap, zero: 0)
        ecgRaw = Ring(capacity: cap, zero: 0)
        ecgBp = Ring(capacity: cap, zero: 0)
        ecgDisp = Ring(capacity: cap, zero: 0)
        filter = EcgFilter(fs: fs)
        qrs = QRSDetector(fs: fs)
        template = BeatTemplate(fs: fs)
        cleaner = RRCleaner()
        pendingTemplate = []
        beats = []
        lastBeatT = nil
        lastBeatIdx = -1
        gapIndex = -1
    }

    private func setupAcc(_ fs: Double) {
        accFs = fs
        let cap = Int(accHistoryS * fs)
        accT = Ring(capacity: cap, zero: 0)
        acc = Ring(capacity: cap, zero: .zero)
        vert = Ring(capacity: cap, zero: 0)
        motion = MotionAnalyzer(fs: fs)
        accRs = UniformResampler(fs: fs)
        decim = max(1, Int((fs / 10).rounded(.toNearestOrEven)))
        decimBuf = []
    }

    public var notchEnabled: Bool {
        get { filter.notchEnabled }
        set { filter.notchEnabled = newValue }
    }

    // MARK: eventi

    public func handle(_ ev: DecodedEvent) {
        switch ev {
        case .ecg(let c): onEcg(c)
        case .acc(let c): onAcc(c)
        case .hr(let h): onHr(h)
        case .battery(let p): metrics.battery = p
        case .streamStarted(let s):
            if s.stream == .ecg && s.sampleRate != ecgFs { setupEcg(s.sampleRate) }
            else if s.stream == .acc && s.sampleRate != accFs { setupAcc(s.sampleRate) }
        }
    }

    private func onEcg(_ ev: EcgChunk) {
        var t = ev.t, x = ev.uv
        if t0 == nil { t0 = t[0] }
        if ev.missingBefore != 0 {
            let dt = 1 / ecgFs
            let n = ev.missingBefore
            if n > 0 && Double(n) * dt <= maxPaddedGapS && ecgRaw.count > 0 {
                // Riempie il buco con l'ultimo valore: l'indice dei campioni resta = tempo
                let lastV = ecgRaw.at(ecgRaw.end - 1)
                t = (0..<n).map { t[0] - dt * Double(n - $0) } + t
                x = Array(repeating: lastV, count: n) + x
                gapIndex = ecgRaw.end + n
            } else {
                resetBeatChain()
            }
        }
        let (bp, disp) = filter.process(x)
        ecgT.extend(t)
        ecgRaw.extend(x)
        ecgBp.extend(bp)
        ecgDisp.extend(disp)

        for b in qrs.push(bp) { onBeat(b) }
        updateTemplates()

        let now = t[t.count - 1]
        if nextTick == nil { nextTick = now + 1 }
        if now >= nextTick! {
            nextTick! += 1
            if now - nextTick! > 5 { nextTick = now + 1 }
            quality.pushEcgSecond(ecgRaw.last(Int(ecgFs)))
            tick(now)
        }
    }

    private func resetBeatChain() {
        filter.reset()
        qrs.reset()
        cleaner.reset()
        pendingTemplate = []
        lastBeatT = nil
        lastBeatIdx = -1
        gapIndex = -1
        ecgT.clear()
        ecgRaw.clear()
        ecgBp.clear()
        ecgDisp.clear()
        beats = []
    }

    private func indexToTime(_ idx: Double) -> Double {
        var i = Int(idx.rounded(.down))
        i = min(max(i, ecgT.start), ecgT.end - 1)
        return ecgT.at(i) + (idx - Double(i)) / ecgFs
    }

    private func onBeat(_ b: Beat) {
        let t = indexToTime(b.index)
        beats.append((b.index, b.amplitude))
        if beats.count > 256 { beats.removeFirst() }
        metrics.beats += 1
        if let last = lastBeatT {
            let rrMs = (t - last) * 1000
            // Intervallo che attraversa un tratto riempito (pacchetti persi): non affidabile
            let invalid = lastBeatIdx < Double(gapIndex) || rrMs <= 0
            quality.pushOurs(rrMs)
            if !invalid { resp.pushBeat(t: t, amplitude: abs(b.amplitude), rrMs: rrMs) }
            for nn in cleaner.push(t: t, rrMs: rrMs, invalid: invalid) { onNN(nn) }
        }
        lastBeatT = t
        lastBeatIdx = b.index
        pendingTemplate.append(b)
    }

    private func onNN(_ nn: NN) {
        let ok = nn.kind == .normal
        rr.append((nn.t, nn.rr, nn.nn, ok))
        if let i = rr.firstIndex(where: { nn.t - $0.t <= rrHistoryS }), i > 0 { rr.removeFirst(i) }
        if nn.kind != .invalid {
            for w in [hrvWin, hrvLong, dfaWin] { w.push(t: nn.t, nn: nn.nn, corrected: !ok) }
        }
        if keepLog {
            beatLog.append(BeatLogRow(t: nn.t - (t0 ?? 0), rrMs: nn.rr, nnMs: nn.nn, kind: nn.kind))
        }
    }

    private func updateTemplates() {
        var keep: [Beat] = []
        for b in pendingTemplate {
            if b.index + Double(template.post) + 2 >= Double(ecgBp.end) {
                keep.append(b)
                continue
            }
            let a = Int(b.index.rounded(.down)) - template.pre - 1
            if a < ecgBp.start { continue }
            let src = ecgBp.get(a, a + template.length + 3)
            if let seg = template.segment(src, rIndexInX: b.index - Double(a)) {
                quality.pushCorr(template.add(seg))
            }
        }
        pendingTemplate = keep
    }

    private func onAcc(_ ev: AccChunk) {
        if t0 == nil { t0 = ev.t[0] }
        if ev.missingBefore != 0 { accRs.reset() }   // non si interpola attraverso un buco
        // Da qui in poi l'accelerometro è esattamente a accFs, qualunque sia il quarzo della fascia
        let (t, mg) = accRs.push(ev.t, ev.mg)
        if t.isEmpty { return }
        let v = motion.push(mg)
        accT.extend(t)
        acc.extend(mg)
        vert.extend(v)
        let buf = decimBuf + mg
        let n = (buf.count / decim) * decim
        if n > 0 {
            let dec = stride(from: 0, to: n, by: decim).map { i in
                buf[i..<(i + decim)].reduce(SIMD3<Double>.zero, +) / Double(decim)
            }
            resp.pushAcc(dec)
        }
        decimBuf = Array(buf[n...])
    }

    private func onHr(_ ev: HrEvent) {
        metrics.hrStrap = Double(ev.bpm)
        metrics.contact = ev.contact
        quality.contact = ev.contact
        let t = ev.deviceT
        // Gli RR della notifica precedono l'istante di arrivo: si ricostruiscono all'indietro
        var accMs = 0.0
        var times: [Double] = []
        for r in ev.rrMs.reversed() {
            times.append(t - accMs / 1000)
            accMs += r
        }
        for (tt, r) in zip(times.reversed(), ev.rrMs) {
            if tt.isFinite { strapRR.append((tt, r)) }
            quality.pushStrap(r)
        }
        if let i = strapRR.firstIndex(where: { t - $0.t <= rrHistoryS }), i > 0 { strapRR.removeFirst(i) }
    }

    // MARK: metriche

    private func tick(_ now: Double) {
        tickCount += 1
        var m = metrics
        let (_, nn, corrected) = hrvWin.arrays()
        if let td = timeDomain(nn) {
            (m.meanNN, m.rmssd, m.sdnn, m.pnn50, m.sd1, m.sd2) = (td.meanNN, td.rmssd, td.sdnn, td.pnn50, td.sd1, td.sd2)
            m.artifactPct = Double(corrected.filter { $0 }.count) / Double(corrected.count) * 100
        }
        if nn.count >= 3 { m.hrEcg = 60000 / median(nn.suffix(5)) }
        if let last = lastBeatT, now - last > 4 { m.hrEcg = .nan }

        let q = quality.evaluate()
        (m.quality, m.qualityLabel, m.templateCorr) = (q.score, q.label, q.templateCorr)
        (m.strapMaeMs, m.strapMatchPct) = (q.strapMaeMs, q.strapMatchPct)

        if tickCount % 5 == 0 {
            let (_, dnn, _) = dfaWin.arrays()
            m.dfaBeats = dnn.count
            m.dfaA1 = dfaWin.spanS >= 60 ? dfaAlpha(dnn) : .nan
            let (tl, nl, _) = hrvLong.arrays()
            if let fd = frequencyDomain(t: tl, nn: nl) { (m.lf, m.hf, m.lfHf) = (fd.lf, fd.hf, fd.lfHf) }
        }
        if tickCount % 2 == 0 {
            let r = resp.estimate()
            (m.respBrpm, m.respSource, m.respConf) = (r.brpm, r.source, r.confidence)
        }
        if acc.count > 0 {
            let ms = motion.update()
            (m.cadenceSpm, m.motionMg, m.verticalOscCm) = (ms.cadenceSpm, ms.intensityMg, ms.verticalOscCm)
        }
        metrics = m
        let rel = now - (t0 ?? 0)
        if keepLog { metricsLog.append(m.csvRow(t: rel)) }
        onTick?(m, rel)
    }

    // MARK: uscite

    public func snapshot(ecgSeconds: Double = 6, rrCount: Int = 120) -> Snapshot {
        var s = Snapshot(metrics: metrics, ecgFs: ecgFs, ecgEnd: ecgRaw.end)
        let n = min(ecgDisp.count, Int(ecgSeconds * ecgFs))
        if n > 0 {
            s.ecg = ecgDisp.last(n)
            s.now = ecgT.at(ecgT.end - 1) - (t0 ?? 0)
            let lo = Double(ecgRaw.end - n)
            s.beatIdx = beats.filter { $0.idx >= lo }.map(\.idx)
        }
        s.rrNN = rr.suffix(rrCount).map(\.nn)
        return s
    }

    /// Fine sessione offline: emette gli ultimi intervalli rimasti in coda al correttore.
    public func finish() {
        for nn in cleaner.flush() { onNN(nn) }
    }
}
