// Correzione degli artefatti RR: Lipponen & Tarvainen (2019), versione causale
// (polarlab/dsp/rr_clean.py). Ogni intervallo si classifica con 2 battiti di ritardo.

import Foundation

private let alpha = 5.2
private let c1 = 0.13, c2 = 0.17
private let qdWindow = 91
private let medWindow = 11
// Soglie minime (ms): in corsa la variabilità è così bassa che la sola quartile deviation
// scarterebbe battiti normali per differenze di pochi millisecondi.
private let minTh1Ms = 15.0, minTh2Ms = 15.0
private let warmup = 15

public enum BeatKind: String {
    case normal, ectopic, missed, extra, longshort, invalid
}

public struct NN {
    public var t: Double      // s, istante del battito che chiude l'intervallo
    public var rr: Double     // ms, intervallo originale
    public var nn: Double     // ms, intervallo corretto (uguale a rr se normale)
    public var kind: BeatKind
}

private func qd(_ x: [Double]) -> Double {
    let s = x.sorted()
    return 0.5 * (percentileSorted(s, 75) - percentileSorted(s, 25))
}

public final class RRCleaner {
    private let maxLen = qdWindow + 3
    private var t: [Double] = [], rr: [Double] = [], forced: [Bool] = []
    private var pending = 0
    private var skipNext = false     // dopo un battito "extra" il successivo è già stato fuso

    public init() {}

    public func reset() {
        t = []
        rr = []
        forced = []
        pending = 0
        skipNext = false
    }

    /// Aggiunge un intervallo; ritorna gli intervalli classificati (con 2 battiti di ritardo).
    public func push(t tt: Double, rrMs: Double, invalid: Bool = false) -> [NN] {
        t.append(tt)
        rr.append(rrMs)
        forced.append(invalid)
        if rr.count > maxLen {
            t.removeFirst()
            rr.removeFirst()
            forced.removeFirst()
        }
        pending += 1
        var out: [NN] = []
        while pending >= 3 {
            let k = rr.count - pending
            pending -= 1
            if skipNext {
                skipNext = false
                continue
            }
            out += classify(k)
        }
        return out
    }

    /// Fine registrazione: emette gli ultimi intervalli senza classificarli.
    public func flush() -> [NN] {
        var out: [NN] = []
        let n = rr.count
        for k in (n - pending)..<n {
            if !skipNext { out.append(NN(t: t[k], rr: rr[k], nn: rr[k], kind: forced[k] ? .invalid : .normal)) }
            skipNext = false
        }
        pending = 0
        return out
    }

    private func classify(_ k: Int) -> [NN] {
        let tk = t[k], x = rr[k]
        if forced[k] {
            return [NN(t: tk, rr: x, nn: median(rr[max(0, k - 5)...k]), kind: .invalid)]
        }
        if k < warmup {
            // Poca storia: quartile deviation inaffidabile. Si scartano solo gli errori grossolani.
            let med = median(rr[0..<(k + 3)])
            return abs(x - med) > 0.3 * med ? [NN(t: tk, rr: x, nn: med, kind: .longshort)]
                                           : [NN(t: tk, rr: x, nn: x, kind: .normal)]
        }
        var drr = [Double](repeating: 0, count: rr.count)
        for i in 1..<rr.count { drr[i] = rr[i] - rr[i - 1] }
        let hist = max(1, k - qdWindow + 3)..<(k + 3)
        let th1 = max(alpha * qd(hist.map { abs(drr[$0]) }), minTh1Ms)

        func med11(_ j: Int) -> Double { median(rr[max(0, j - medWindow + 3)..<(j + 3)]) }
        let med = med11(k)
        let lo = max(0, k - qdWindow + 3)
        let mrrHist = (lo...k).map { j -> Double in
            let d = rr[j] - med11(j)
            return abs(d < 0 ? 2 * d : d)
        }
        let th2 = max(alpha * qd(mrrHist), minTh2Ms)
        let mrr = x - med
        let mrrs = (mrr < 0 ? 2 * mrr : mrr) / th2

        let d0 = drr[k] / th1, dm1 = drr[k - 1] / th1, dp1 = drr[k + 1] / th1, dp2 = drr[k + 2] / th1
        let s12 = d0 > 0 ? max(dm1, dp1) : min(dm1, dp1)
        let s22 = d0 >= 0 ? min(dp1, dp2) : max(dp1, dp2)

        if abs(d0) > 1 && ((d0 > 1 && s12 < -c1 * d0 - c2) || (d0 < -1 && s12 > -c1 * d0 + c2)) {
            return [NN(t: tk, rr: x, nn: med, kind: .ectopic)]
        }
        if !(abs(d0) > 1 || abs(mrrs) > 3) { return [NN(t: tk, rr: x, nn: x, kind: .normal)] }

        let longBeat = d0 > 1 && s22 < -1
        let shortBeat = d0 < -1 && s22 > 1
        if !(longBeat || shortBeat || abs(mrrs) > 3) { return [NN(t: tk, rr: x, nn: x, kind: .normal)] }
        // Battito mancato: l'intervallo è circa il doppio della mediana
        if longBeat && abs(x / 2 - med) < th2 {
            return [NN(t: tk - x / 2000.0, rr: x / 2, nn: x / 2, kind: .missed), NN(t: tk, rr: x / 2, nn: x / 2, kind: .missed)]
        }
        // Battito in più: questo intervallo + il successivo ≈ mediana → si fondono
        if shortBeat && abs(x + rr[k + 1] - med) < th2 {
            skipNext = true
            return [NN(t: t[k + 1], rr: x + rr[k + 1], nn: x + rr[k + 1], kind: .extra)]
        }
        return [NN(t: tk, rr: x, nn: med, kind: .longshort)]
    }
}
