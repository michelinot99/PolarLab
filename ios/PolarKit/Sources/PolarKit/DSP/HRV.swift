// HRV su finestra mobile e DFA α1 (polarlab/dsp/hrv.py, polarlab/dsp/dfa.py).

import Foundation

public let lfBand = (0.04, 0.15)
public let hfBand = (0.15, 0.40)
public let aetAlpha1 = 0.75     // soglia aerobica (VT1), Rogers et al. 2021
public let antAlpha1 = 0.50     // vicino alla soglia anaerobica (VT2)

public struct TimeDomain {
    public var meanNN, hr, sdnn, rmssd, pnn50, sd1, sd2: Double
}

public struct FreqDomain {
    public var lf, hf, lfHf, respPeakHz: Double
}

/// Finestra temporale scorrevole di (t, nn, corretto?).
public final class NNWindow {
    public let windowS: Double
    private var data: [(t: Double, nn: Double, corrected: Bool)] = []
    private var head = 0

    public init(windowS: Double) { self.windowS = windowS }

    public func push(t: Double, nn: Double, corrected: Bool) {
        data.append((t, nn, corrected))
        while head < data.count, t - data[head].t > windowS { head += 1 }
        if head > 1024 {
            data.removeFirst(head)
            head = 0
        }
    }

    public func clear() {
        data = []
        head = 0
    }

    public var count: Int { data.count - head }

    public func arrays() -> (t: [Double], nn: [Double], corrected: [Bool]) {
        let s = data[head...]
        return (s.map(\.t), s.map(\.nn), s.map(\.corrected))
    }

    public var spanS: Double { count > 1 ? data[data.count - 1].t - data[head].t : 0 }
}

public func timeDomain(_ nn: [Double]) -> TimeDomain? {
    if nn.count < 5 { return nil }
    let d = diff(nn)
    let sdnn = std(nn, ddof: 1)
    let rmssd = (d.reduce(0) { $0 + $1 * $1 } / Double(d.count)).squareRoot()
    // Poincaré: SD1 perpendicolare alla diagonale, SD2 lungo la diagonale
    let sd1 = 0.5.squareRoot() * std(d, ddof: 1)
    let sd2 = max(0, 2 * sdnn * sdnn - sd1 * sd1).squareRoot()
    let m = mean(nn)
    let pnn50 = Double(d.filter { abs($0) > 50 }.count) / Double(d.count) * 100
    return TimeDomain(meanNN: m, hr: 60000 / m, sdnn: sdnn, rmssd: rmssd, pnn50: pnn50, sd1: sd1, sd2: sd2)
}

/// np.arange(a, b, step) per step positivi.
func arange(_ a: Double, _ b: Double, _ step: Double) -> [Double] {
    let n = max(0, Int(((b - a) / step).rounded(.up)))
    return (0..<n).map { a + Double($0) * step }
}

/// Ricampionamento cubico a 4 Hz + Welch; bande LF/HF classiche (Task Force 1996).
public func frequencyDomain(t: [Double], nn: [Double], fsResample: Double = 4.0) -> FreqDomain? {
    guard nn.count >= 30, t[t.count - 1] - t[0] >= 60 else { return nil }
    let tu = arange(t[0], t[t.count - 1], 1.0 / fsResample)
    let y = detrendLinear(cubicSpline(t, nn)(tu))
    let nper = min(y.count, Int(256 * fsResample / 4))
    let (f, p) = welch(y, fs: fsResample, nperseg: nper, noverlap: nper / 2, detrendConstant: false)

    func band(_ lo: Double, _ hi: Double) -> Double {
        let idx = f.indices.filter { f[$0] >= lo && f[$0] < hi }
        return idx.isEmpty ? 0 : trapezoid(idx.map { p[$0] }, idx.map { f[$0] })
    }
    let lf = band(lfBand.0, lfBand.1), hf = band(hfBand.0, hfBand.1)
    let beatNyq = 0.5 * 1000 / mean(nn)
    let m = f.indices.filter { f[$0] >= 0.1 && f[$0] <= min(1.0, 0.9 * beatNyq) }
    var peak = Double.nan
    if var best = m.first {
        for i in m where p[i] > p[best] { best = i }
        peak = f[best]
    }
    return FreqDomain(lf: lf, hf: hf, lfHf: hf > 0 ? lf / hf : .nan, respPeakHz: peak)
}

// MARK: - DFA

/// F(n): RMS dei residui dopo detrend lineare in finestre non sovrapposte, prese sia
/// dall'inizio sia dalla fine della serie.
func fluctuation(_ rr: [Double], scales: [Int]) -> [Double] {
    let m = mean(rr)
    let y = cumsum(rr.map { $0 - m })
    return scales.map { n in
        let nseg = y.count / n
        if nseg < 2 { return .nan }
        let xs = (0..<n).map { Double($0) - Double(n - 1) / 2 }
        let xx = xs.reduce(0) { $0 + $1 * $1 }
        var ss = 0.0
        for start in (0..<nseg).map({ $0 * n }) + (0..<nseg).map({ y.count - nseg * n + $0 * n }) {
            let seg = y[start..<(start + n)]
            let sm = mean(seg)
            var sxy = 0.0
            for (i, v) in seg.enumerated() { sxy += (v - sm) * xs[i] }
            let slope = sxy / xx
            for (i, v) in seg.enumerated() {
                let r = (v - sm) - slope * xs[i]
                ss += r * r
            }
        }
        return (ss / Double(2 * nseg * n)).squareRoot()
    }
}

public func dfaAlpha(_ rr: [Double], nMin: Int = 4, nMax: Int = 16) -> Double {
    if rr.count < 2 * nMax { return .nan }
    let scales = Array(nMin...nMax)
    let f = fluctuation(rr, scales: scales)
    let ok = scales.indices.filter { f[$0].isFinite && f[$0] > 0 }
    if ok.count < 3 { return .nan }
    return polyfitSlope(ok.map { log(Double(scales[$0])) }, ok.map { log(f[$0]) })
}

public enum Alpha1Zone: String {
    case none = "—", aerobic = "aerobico", between = "tra soglie", aboveVT2 = "sopra VT2"

    public init(_ a: Double) {
        if !a.isFinite { self = .none } else if a >= aetAlpha1 { self = .aerobic } else if a >= antAlpha1 { self = .between } else { self = .aboveVT2 }
    }
}
