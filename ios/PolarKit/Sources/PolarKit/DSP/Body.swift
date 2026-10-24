// Respiro, movimento, qualità del segnale e battito medio
// (polarlab/dsp/respiration.py, motion.py, quality.py, template.py).

import Foundation

// MARK: - Respirazione

private let respBand = (0.1, 1.0)
private let respFs = 4.0

public struct RespEstimate {
    public var brpm: Double
    public var confidence: Double
    public var source: String       // "acc+ecg", "ecg", "acc", "—"
}

private func spectralPeak(_ x: [Double], fs: Double, hi: Double) -> (Double, Double) {
    if Double(x.count) < 8 * fs { return (.nan, 0) }
    let xd = detrendLinear(x)
    let nper = min(xd.count, Int(32 * fs))
    let (f, p) = welch(xd, fs: fs, nperseg: nper, noverlap: nper / 2, detrendConstant: true)
    let m = f.indices.filter { f[$0] >= respBand.0 && f[$0] <= hi }
    let pm = m.map { p[$0] }, fm = m.map { f[$0] }
    let total = pm.reduce(0, +)
    if m.count < 3 || total <= 0 { return (.nan, 0) }
    var k = 0
    for i in 1..<pm.count where pm[i] > pm[k] { k = i }
    // Prominenza: frazione di potenza di banda attorno al picco (±1 bin)
    let prom = pm[max(0, k - 1)..<min(pm.count, k + 2)].reduce(0, +) / total
    var freq = fm[k]
    if k > 0 && k < pm.count - 1 {
        let y0 = log(pm[k - 1] + 1e-12), y1 = log(pm[k] + 1e-12), y2 = log(pm[k + 1] + 1e-12)
        let den = y0 - 2 * y1 + y2
        let off = den < 0 ? 0.5 * (y0 - y2) / den : 0
        freq = fm[k] + off * (fm[1] - fm[0])
    }
    return (freq, prom)
}

public final class RespirationEstimator {
    let windowS: Double
    let accFs: Double
    private var beats: [(t: Double, amp: Double, rr: Double)] = []
    private let acc: Ring<SIMD3<Double>>

    public init(accFsDecimated: Double = 10.0, windowS: Double = 60.0) {
        self.windowS = windowS
        accFs = accFsDecimated
        acc = Ring(capacity: Int(windowS * accFsDecimated), zero: .zero)
    }

    public func pushBeat(t: Double, amplitude: Double, rrMs: Double) {
        beats.append((t, amplitude, rrMs))
        if let i = beats.firstIndex(where: { t - $0.t <= windowS }), i > 0 { beats.removeFirst(i) }
    }

    public func pushAcc(_ mg: [SIMD3<Double>]) { acc.extend(mg) }

    public func estimate() -> RespEstimate {
        var est: [(String, (Double, Double))] = []
        if beats.count >= 20 {
            let t = beats.map(\.t)
            let beatNyq = 0.5 * 1000 / median(beats.map(\.rr))
            let hi = min(respBand.1, 0.85 * beatNyq)
            if diff(t).allSatisfy({ $0 > 0 }) && t[t.count - 1] - t[0] > 20 {
                let tu = arange(t[0], t[t.count - 1], 1 / respFs)
                est.append(("edr", spectralPeak(pchip(t, beats.map(\.amp))(tu), fs: respFs, hi: hi)))
                est.append(("rsa", spectralPeak(pchip(t, beats.map(\.rr))(tu), fs: respFs, hi: hi)))
            }
        }
        if Double(acc.count) >= 20 * accFs {
            var a = acc.last(acc.count)
            let m = a.reduce(SIMD3<Double>.zero, +) / Double(a.count)
            a = a.map { $0 - m }
            // Componente principale del movimento lento del torace (primo vettore singolare)
            var c = [[Double]](repeating: [0, 0, 0], count: 3)
            for v in a { for i in 0..<3 { for j in 0..<3 { c[i][j] += v[i] * v[j] } } }
            let axis = principalAxis(c)
            let pc = a.map { ($0 * axis).sum() }
            let lp = butter(2, [respBand.1], .lowpass, fs: accFs)
            est.append(("acc", spectralPeak(sosfiltfilt(lp, pc), fs: accFs, hi: respBand.1)))
        }
        return Self.fuse(est)
    }

    static func fuse(_ est: [(String, (Double, Double))]) -> RespEstimate {
        let valid = est.filter { $0.1.0.isFinite && $0.1.1 > 0.15 }
        guard var best = valid.first else { return RespEstimate(brpm: .nan, confidence: 0, source: "—") }
        for v in valid.dropFirst() where v.1.1 > best.1.1 { best = v }
        let fBest = best.1.0
        let agree = valid.filter { abs($0.1.0 - fBest) * 60 <= 3 }
        let w = agree.map(\.1.1)
        let f = zip(agree, w).reduce(0) { $0 + $1.0.1.0 * $1.1 } / w.reduce(0, +)
        let conf = min(1, best.1.1 * (1 + 0.3 * Double(agree.count - 1)))
        let src = Set(agree.map { $0.0 == "acc" ? "acc" : "ecg" }).sorted().joined(separator: "+")
        return RespEstimate(brpm: f * 60, confidence: conf, source: src)
    }
}

// MARK: - Movimento

private let gMs2 = 9.80665

public struct MotionState {
    public var cadenceSpm: Double
    public var cadenceConf: Double
    public var intensityMg: Double
    public var verticalOscCm: Double
}

public final class MotionAnalyzer {
    let fs: Double
    private let buf: Ring<SIMD3<Double>>
    private let vert: Ring<Double>
    private let lp: [SosFilter]

    public init(fs: Double, windowS: Double = 8.0) {
        self.fs = fs
        buf = Ring(capacity: Int(windowS * fs) + 1, zero: .zero)
        vert = Ring(capacity: Int(windowS * fs) + 1, zero: 0)
        let sos = butter(2, [0.3], .lowpass, fs: fs)
        lp = (0..<3).map { _ in SosFilter(sos) }
    }

    /// Aggiunge campioni in mG; ritorna l'accelerazione verticale dinamica (mG).
    public func push(_ mg: [SIMD3<Double>]) -> [Double] {
        if mg.isEmpty { return [] }
        // Un accelerometro fermo misura +1 g verso l'alto: la componente lenta indica la verticale
        let g = (0..<3).map { i in lp[i].process(mg.map { $0[i] }) }
        var out = [Double](repeating: 0, count: mg.count)
        for k in 0..<mg.count {
            let gv = SIMD3(g[0][k], g[1][k], g[2][k])
            let norm = (gv * gv).sum().squareRoot()
            let up = gv / max(norm, 1e-6)
            out[k] = (mg[k] * up).sum() - norm
        }
        buf.extend(mg)
        vert.extend(out)
        return out
    }

    public func update() -> MotionState {
        let v = vert.last(vert.count)
        let dyn = buf.last(buf.count)
        var intensity = 0.0
        if !dyn.isEmpty {
            let m = dyn.reduce(SIMD3<Double>.zero, +) / Double(dyn.count)
            intensity = (dyn.reduce(0) { $0 + (($1 - m) * ($1 - m)).sum() } / Double(dyn.count)).squareRoot()
        }
        var cad = Double.nan, conf = 0.0, osc = Double.nan
        if Double(v.count) >= 4 * fs && intensity > 80 {
            let (c, cf, period) = cadence(v)
            cad = c
            conf = cf
            if period.isFinite { osc = verticalOscillation(v, period: period) }
        }
        return MotionState(cadenceSpm: cad, cadenceConf: conf, intensityMg: intensity, verticalOscCm: osc)
    }

    private func cadence(_ v: [Double]) -> (Double, Double, Double) {
        var x = sosfiltfilt(butter(2, [1.0, 6.0], .bandpass, fs: fs), v)
        let m = mean(x)
        x = x.map { $0 - m }
        let lo = Int(0.25 * fs)
        let hi = min(Int(0.75 * fs), x.count - 2)   // 80–240 passi/min
        // Autocorrelazione solo sui ritardi che servono
        let maxLag = min(x.count - 1, hi + 2)
        var ac = [Double](repeating: 0, count: maxLag + 1)
        for lag in 0...maxLag {
            var s = 0.0
            for i in 0..<(x.count - lag) { s += x[i + lag] * x[i] }
            ac[lag] = s
        }
        if ac[0] <= 0 { return (.nan, 0, .nan) }
        let a0 = ac[0]
        ac = ac.map { $0 / a0 }
        guard hi >= lo else { return (.nan, 0, .nan) }
        let seg = Array(ac[lo...hi])
        let peaks = findPeaks(seg)
        guard var bestPeak = peaks.first else { return (.nan, 0, .nan) }
        for p in peaks.dropFirst() where seg[p] > seg[bestPeak] { bestPeak = p }
        var k = lo + bestPeak
        // Se c'è un picco forte a metà ritardo, k è la falcata (2 passi): scegli il passo
        let half = Int((Double(k) / 2).rounded(.toNearestOrEven))
        if half >= lo, ac[(half - 1)...(half + 1)].max()! > 0.6 * ac[k] {
            var j = half - 1
            for i in half...(half + 1) where ac[i] > ac[j] { j = i }
            k = j
        }
        let (y0, y1, y2) = (ac[k - 1], ac[k], ac[k + 1])
        let den = y0 - 2 * y1 + y2
        let frac = den < 0 ? 0.5 * (y0 - y2) / den : 0
        let period = (Double(k) + frac) / fs
        if ac[k] < 0.3 { return (.nan, ac[k], .nan) }
        return (60 / period, ac[k], period)
    }

    /// Doppia integrazione dell'accelerazione verticale con passa-alto tra le integrazioni.
    private func verticalOscillation(_ v: [Double], period: Double) -> Double {
        let hp = butter(2, [0.5 / period * 0.5], .highpass, fs: fs)
        let a = sosfiltfilt(hp, v.map { $0 * gMs2 / 1000 })
        let vel = sosfiltfilt(hp, cumsum(a).map { $0 / fs })
        let pos = sosfiltfilt(hp, cumsum(vel).map { $0 / fs })
        let n = Int((period * fs).rounded(.toNearestOrEven))
        let tail = pos.count > 3 * n ? Array(pos[n..<(pos.count - n)]) : pos
        if tail.count < n || n == 0 { return .nan }
        let p2p = (0..<(tail.count / n)).map { i -> Double in
            let s = tail[(i * n)..<((i + 1) * n)]
            return s.max()! - s.min()!
        }
        return median(p2p) * 100
    }
}

// MARK: - Qualità del segnale

private let saturationUv = 20_000.0

public struct Quality {
    public var score: Double
    public var label: String
    public var templateCorr: Double
    public var strapMaeMs: Double
    public var strapMatchPct: Double
}

/// Allinea due serie RR senza timestamp comuni cercando lo sfasamento (in battiti) migliore.
func alignRR(_ ours: [Double], _ strap: [Double], maxLag: Int = 6) -> (Double, Double) {
    var best = (Double.nan, Double.nan)
    if ours.count < 8 || strap.count < 8 { return best }
    var bestMae = Double.infinity
    for lag in -maxLag...maxLag {
        let a = lag > 0 ? Array(ours.prefix(ours.count - lag)) : ours
        let b = lag < 0 ? Array(strap.prefix(strap.count + lag)) : strap
        let n = min(a.count, b.count)
        if n < 6 { continue }
        let err = zip(a.suffix(n), b.suffix(n)).map { abs($0 - $1) }
        let mae = mean(err)
        if mae < bestMae {
            bestMae = mae
            best = (mae, Double(err.filter { $0 <= 10 }.count) / Double(n) * 100)
        }
    }
    return best
}

public final class QualityMonitor {
    private(set) var corrs: [Double] = []
    var ours: [Double] = []
    var strap: [Double] = []
    var contact: Bool?
    private var flat = false

    func pushOurs(_ rr: Double) { ours.append(rr); if ours.count > 40 { ours.removeFirst() } }
    func pushStrap(_ rr: Double) { strap.append(rr); if strap.count > 40 { strap.removeFirst() } }

    func pushCorr(_ c: Double) {
        guard c.isFinite else { return }
        corrs.append(c)
        if corrs.count > 20 { corrs.removeFirst() }
    }

    func pushEcgSecond(_ x: [Double]) {
        guard !x.isEmpty else { return }
        flat = std(x) < 5 || Double(x.filter { abs($0) > saturationUv }.count) / Double(x.count) > 0.05
    }

    func evaluate() -> Quality {
        let corr = corrs.isEmpty ? Double.nan : median(corrs)
        let (mae, match) = alignRR(ours, strap)
        var parts: [Double] = [], weights: [Double] = []
        if corr.isFinite {
            parts.append(min(max((corr - 0.5) / 0.45, 0), 1))
            weights.append(0.5)
        }
        if match.isFinite {
            parts.append(match / 100)
            weights.append(0.35)
        }
        let bad = flat || contact == false
        if parts.isEmpty {
            return Quality(score: bad ? 0 : .nan, label: bad ? "inutilizzabile" : "—", templateCorr: corr,
                           strapMaeMs: mae, strapMatchPct: match)
        }
        parts.append(bad ? 0 : 1)
        weights.append(0.15)
        var score = 100 * zip(parts, weights).reduce(0) { $0 + $1.0 * $1.1 } / weights.reduce(0, +)
        if bad { score = min(score, 10) }
        let label = score >= 85 ? "ottima" : score >= 65 ? "buona" : score >= 35 ? "scarsa" : "inutilizzabile"
        return Quality(score: score, label: label, templateCorr: corr, strapMaeMs: mae, strapMatchPct: match)
    }
}

// MARK: - Battito medio

public final class BeatTemplate {
    let fs: Double
    let pre, post, length: Int
    public let tAxis: [Double]
    let initBeats = 8, alpha = 0.08, minCorr = 0.7
    public private(set) var mean: [Double]?
    private var count = 0

    public init(fs: Double) {
        self.fs = fs
        pre = Int((0.25 * fs).rounded(.toNearestOrEven))
        post = Int((0.45 * fs).rounded(.toNearestOrEven))
        length = pre + post + 1
        let p = pre
        tAxis = (0..<length).map { Double($0 - p) / fs }
    }

    public func reset() {
        mean = nil
        count = 0
    }

    /// Segmento centrato sul picco R con allineamento sub-campione.
    func segment(_ x: [Double], rIndexInX: Double) -> [Double]? {
        let pos = (-pre...post).map { rIndexInX + Double($0) }
        if pos[0] < 0 || pos[pos.count - 1] > Double(x.count - 1) { return nil }
        return interp(pos, (0..<x.count).map(Double.init), x)
    }

    /// Aggiorna il template; ritorna la correlazione del battito col template precedente.
    func add(_ seg: [Double]) -> Double {
        guard var m = mean else {
            mean = seg
            count = 1
            return .nan
        }
        let corr = corrcoef(seg, m)
        if count < initBeats {
            count += 1
            for i in 0..<m.count { m[i] += (seg[i] - m[i]) / Double(count) }
        } else if corr >= minCorr {
            for i in 0..<m.count { m[i] += alpha * (seg[i] - m[i]) }
        }
        mean = m
        return corr
    }
}
