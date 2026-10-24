// Rilevatore QRS in tempo reale: Pan & Tompkins adattato a 130 Hz (polarlab/dsp/qrs.py).
// Il picco R si localizza sul segnale filtrato con interpolazione gaussiana: a 130 Hz un
// campione vale 7.7 ms, troppo per l'HRV.

import Foundation

public struct Beat {
    public var index: Double      // posizione sub-campione (indice assoluto nello stream ECG)
    public var amplitude: Double  // µV sul segnale filtrato, con segno
    public var searchback: Bool
}

public final class QRSDetector {
    let fs: Double
    private let bp: SosFilter
    private let deriv: FirFilter
    private let mwiN: Int
    private let mwi: FirFilter
    private let x: Ring<Double>
    private let slope: Ring<Double>
    private let learnN, refractory, tWaveWin, lookahead, searchLo, relearnAfter: Int

    private var n = 0
    private var learningUntil = 0, quietSince = 0
    private var learnMax = 0.0, learnSum = 0.0, learnCount = 0
    private(set) var spki = 0.0, npki = 0.0
    private var candIdx = -1, candVal = 0.0, prevVal = 0.0
    private var lastQrs: Int?
    private var lastSlope = 0.0
    private var rrRecent: [Int] = [], rrSelected: [Int] = []
    private var rrAvg2: Double?
    private var noisePeaks: [(Int, Double)] = []
    public private(set) var polarity = 1.0
    private var polarityVotes = 0.0

    public init(fs: Double = 130.0, learningS: Double = 2.0) {
        self.fs = fs
        bp = SosFilter(bandpassSOS(fs: fs, lo: 5.0, hi: 15.0))
        deriv = FirFilter([2.0, 1.0, 0.0, -1.0, -2.0].map { $0 * fs / 8.0 })
        mwiN = max(1, Int((0.150 * fs).rounded(.toNearestOrEven)))
        mwi = FirFilter(Array(repeating: 1.0 / Double(mwiN), count: mwiN))
        let cap = Int(4 * fs)
        x = Ring(capacity: cap, zero: 0)
        slope = Ring(capacity: cap, zero: 0)
        learnN = Int(learningS * fs)
        refractory = Int(0.200 * fs)
        tWaveWin = Int(0.360 * fs)
        lookahead = Int(0.200 * fs)
        searchLo = Int(0.300 * fs)
        relearnAfter = Int(3.0 * fs)
        reset()
    }

    public func reset() {
        bp.reset()
        deriv.reset()
        mwi.reset()
        x.clear()
        slope.clear()
        n = 0
        beginLearning(0)
        lastQrs = nil
        lastSlope = 0
        rrRecent = []
        rrSelected = []
        rrAvg2 = nil
        noisePeaks = []
        polarity = 1
        polarityVotes = 0
    }

    private func beginLearning(_ at: Int) {
        learningUntil = at + learnN
        quietSince = learningUntil
        learnMax = 0
        learnSum = 0
        learnCount = 0
        spki = 0
        npki = 0
        candIdx = -1
        candVal = 0
        prevVal = 0
    }

    var threshold1: Double { npki + 0.25 * (spki - npki) }
    var threshold2: Double { 0.5 * threshold1 }

    /// `xs`: ECG già filtrato 0.5–40 Hz (µV). Ritorna i battiti confermati in questo blocco.
    public func push(_ xs: [Double]) -> [Beat] {
        if xs.isEmpty { return [] }
        let n0 = n
        x.extend(xs)
        let d = deriv.process(bp.process(xs))
        slope.extend(d.map { abs($0) })
        let m = mwi.process(d.map { $0 * $0 })
        n += xs.count

        var beats: [Beat] = []
        for (i, v) in m.enumerated() {
            let idx = n0 + i
            if idx < learningUntil {
                learn(idx, v)
                continue
            }
            // Ricerca dei massimi locali con 200 ms di "lookahead"
            if v > candVal && (candIdx >= 0 || v > prevVal) {
                candIdx = idx
                candVal = v
            } else if candIdx >= 0 && idx - candIdx >= lookahead {
                if let b = classify(candIdx, candVal) { beats.append(b) }
                candIdx = -1
                candVal = 0
            }
            prevVal = v

            if let b = maybeSearchback(idx) { beats.append(b) }
            if idx - quietSince > relearnAfter {
                // Nessun battito da 3 s (contatto perso, ampiezza cambiata): riapprendi le soglie
                beginLearning(idx + 1)
                lastQrs = nil
                rrAvg2 = nil
                noisePeaks = []
            }
        }
        return beats.sorted { $0.index < $1.index }
    }

    private func learn(_ idx: Int, _ v: Double) {
        learnMax = max(learnMax, v)
        learnSum += v
        learnCount += 1
        if idx == learningUntil - 1 {
            quietSince = idx
            spki = learnMax / 3.0
            npki = 0.5 * learnSum / Double(max(1, learnCount))
            let seg = x.last(min(learnN, x.count))
            if !seg.isEmpty {
                let s = seg.sorted()
                let hi = percentileSorted(s, 99.5), lo = percentileSorted(s, 0.5)
                polarity = hi >= -lo ? 1 : -1
            }
        }
    }

    private func classify(_ idx: Int, _ val: Double) -> Beat? {
        if let last = lastQrs, idx - last < refractory {
            npki = 0.125 * val + 0.875 * npki
            return nil
        }
        if val > threshold1 {
            let s = maxSlope(idx)
            if let last = lastQrs, idx - last < tWaveWin, s < 0.5 * lastSlope {
                // Probabile onda T: pendenza troppo bassa rispetto al QRS precedente
                npki = 0.125 * val + 0.875 * npki
                return nil
            }
            spki = 0.125 * val + 0.875 * spki
            return accept(idx, s, searchback: false)
        }
        npki = 0.125 * val + 0.875 * npki
        noisePeaks.append((idx, val))
        if noisePeaks.count > 64 { noisePeaks.removeFirst(noisePeaks.count - 64) }
        return nil
    }

    private func maybeSearchback(_ now: Int) -> Beat? {
        guard let avg = rrAvg2, let last = lastQrs, Double(now - last) > 1.66 * avg else { return nil }
        let th2 = threshold2
        let cands = noisePeaks.filter { $0.1 > th2 && $0.0 - last >= refractory }
        // np.argmax: a parità vince il primo
        guard var best = cands.first else { return nil }
        for c in cands.dropFirst() where c.1 > best.1 { best = c }
        spki = 0.25 * best.1 + 0.75 * spki
        return accept(best.0, maxSlope(best.0), searchback: true)
    }

    private func accept(_ idx: Int, _ s: Double, searchback: Bool) -> Beat? {
        if let last = lastQrs {
            let rr = idx - last
            rrRecent.append(rr)
            if rrRecent.count > 8 { rrRecent.removeFirst() }
            let inRange = { (r: Int) -> Bool in
                guard let a = self.rrAvg2 else { return true }
                return 0.92 * a <= Double(r) && Double(r) <= 1.16 * a
            }
            if rrAvg2 == nil || inRange(rr) {
                rrSelected.append(rr)
                if rrSelected.count > 8 { rrSelected.removeFirst() }
                rrAvg2 = Double(rrSelected.reduce(0, +)) / Double(rrSelected.count)
            } else if rrRecent.count == 8 && rrRecent.allSatisfy({ !inRange($0) }) {
                // Ritmo cambiato bruscamente: riparti dalla media recente
                rrSelected = rrRecent
                rrAvg2 = Double(rrRecent.reduce(0, +)) / Double(rrRecent.count)
            }
        }
        lastQrs = idx
        quietSince = idx
        lastSlope = s
        noisePeaks = noisePeaks.filter { $0.0 > idx }
        return locateR(idx, searchback)
    }

    private func maxSlope(_ idx: Int) -> Double {
        let a = max(slope.start, idx - mwiN)
        let b = min(slope.end, idx + 1)
        return b > a ? slope.get(a, b).max()! : 0
    }

    private func locateR(_ idx: Int, _ searchback: Bool) -> Beat? {
        let a = max(x.start, idx - searchLo)
        let b = min(x.end, idx + 1)
        if b - a < 3 { return nil }
        let seg = x.get(a, b)
        let s = seg.map { $0 * polarity }
        var k = 0
        for i in 1..<s.count where s[i] > s[k] { k = i }
        // Voto di polarità: se l'estremo opposto è stabilmente più ampio, il QRS è invertito
        polarityVotes = 0.9 * polarityVotes + (-s.min()! > 1.4 * s.max()! ? 1.0 : 0.0)
        if polarityVotes > 5.0 {
            polarity *= -1
            polarityVotes = 0
        }
        var offset = 0.0
        if k > 0 && k < s.count - 1 {
            var (y0, y1, y2) = (s[k - 1], s[k], s[k + 1])
            if min(y0, y1, y2) > 0 {
                // Interpolazione gaussiana (parabola sui logaritmi)
                (y0, y1, y2) = (log(y0), log(y1), log(y2))
            }
            let den = y0 - 2 * y1 + y2
            if den < 0 { offset = min(max(0.5 * (y0 - y2) / den, -0.5), 0.5) }
        }
        return Beat(index: Double(a + k) + offset, amplitude: seg[k], searchback: searchback)
    }
}
