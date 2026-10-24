// Buffer circolare e filtri in streaming (polarlab/core/ringbuffer.py, polarlab/dsp/filters.py).

import Foundation

/// Ultimi `capacity` valori, indicizzati in modo assoluto: `end` è l'indice del prossimo.
public final class Ring<T> {
    private var buf: [T]
    public let capacity: Int
    public private(set) var end = 0

    public init(capacity: Int, zero: T) {
        self.capacity = max(1, capacity)
        buf = Array(repeating: zero, count: self.capacity)
    }

    public var start: Int { max(0, end - capacity) }
    public var count: Int { end - start }

    public func extend<C: Collection>(_ values: C) where C.Element == T {
        // Se il blocco supera la capacità si tengono solo gli ultimi valori, con il loro indice
        end += max(0, values.count - capacity)
        for v in values.suffix(capacity) { buf[end % capacity] = v; end += 1 }
    }

    public func append(_ v: T) {
        buf[end % capacity] = v
        end += 1
    }

    /// Campioni con indice assoluto in [a, b).
    public func get(_ a: Int, _ b: Int) -> [T] {
        precondition(a >= start && b <= end && a <= b, "intervallo [\(a), \(b)) fuori da [\(start), \(end))")
        return (a..<b).map { buf[$0 % capacity] }
    }

    public func last(_ n: Int) -> [T] {
        let n = min(n, count)
        return get(end - n, end)
    }

    public func at(_ i: Int) -> T { buf[i % capacity] }

    public func clear() { end = 0 }
}

/// Filtro IIR in sezioni del secondo ordine; lo stato persiste tra un blocco e l'altro.
public final class SosFilter {
    let sos: [Section]
    private var zi: [[Double]]?

    public init(_ sos: [Section]) { self.sos = sos }

    public func process(_ x: [Double]) -> [Double] {
        if x.isEmpty { return x }
        if zi == nil {
            // Stato iniziale a regime per un ingresso costante: niente transitorio all'avvio
            zi = sosfiltZi(sos).map { $0.map { $0 * x[0] } }
        }
        return sosfilt(sos, x, &zi!)
    }

    public func reset() { zi = nil }
}

/// FIR con stato (lfilter con a = [1]).
public final class FirFilter {
    let b: [Double]
    private var hist: [Double]

    public init(_ taps: [Double]) {
        b = taps
        hist = Array(repeating: 0, count: taps.count - 1)
    }

    public func process(_ x: [Double]) -> [Double] {
        let m = b.count - 1
        let ext = hist + x
        var y = [Double](repeating: 0, count: x.count)
        for i in 0..<x.count {
            var acc = 0.0
            for k in 0...m { acc += b[k] * ext[i + m - k] }
            y[i] = acc
        }
        hist = Array(ext.suffix(m))
        return y
    }

    public func reset() { hist = Array(repeating: 0, count: b.count - 1) }
}

func bandpassSOS(fs: Double, lo: Double, hi: Double, order: Int = 2) -> [Section] {
    butter(order, [lo, hi], .bandpass, fs: fs)
}

/// Passa-banda 0.5–40 Hz + notch di rete opzionale (50 Hz).
public final class EcgFilter {
    private let bp: SosFilter
    private let notch: SosFilter?
    public var notchEnabled = false

    public init(fs: Double, lo: Double = 0.5, hi: Double = 40.0, mainsHz: Double = 50.0) {
        bp = SosFilter(bandpassSOS(fs: fs, lo: lo, hi: min(hi, 0.45 * fs)))
        notch = mainsHz < fs / 2 ? SosFilter(notchSOS(fs: fs, f0: mainsHz)) : nil
    }

    /// (passa-banda, visualizzato). Il primo alimenta il rilevatore QRS.
    public func process(_ x: [Double]) -> (bp: [Double], display: [Double]) {
        let b = bp.process(x)
        guard notchEnabled, let notch else {
            _ = notch?.process(b)
            return (b, b)
        }
        return (b, notch.process(b))
    }

    public func reset() {
        bp.reset()
        notch?.reset()
    }
}

/// Riporta lo stream su una griglia uniforme a `fs` usando i timestamp della fascia:
/// la H10 a "100 Hz" di accelerometro ne produce ~101.9.
public final class UniformResampler {
    let dt: Double
    private var t0: Double?
    private var k = 0
    private var prev: (t: Double, x: SIMD3<Double>)?

    public init(fs: Double) { dt = 1.0 / fs }

    public func reset() {
        t0 = nil
        k = 0
        prev = nil
    }

    public func push(_ t: [Double], _ x: [SIMD3<Double>]) -> ([Double], [SIMD3<Double>]) {
        if t.isEmpty { return ([], []) }
        var tt = t, xx = x
        if let prev {
            // L'ultimo campione del blocco precedente fa da ponte per interpolare
            tt.insert(prev.t, at: 0)
            xx.insert(prev.x, at: 0)
        }
        if t0 == nil { t0 = tt[0] }
        let nEnd = Int(((tt[tt.count - 1] - t0!) / dt + 1e-9).rounded(.down)) + 1
        let grid = (k..<max(k, nEnd)).map { t0! + dt * Double($0) }
        k = max(k, nEnd)
        prev = (tt[tt.count - 1], xx[xx.count - 1])
        var out = [SIMD3<Double>]()
        out.reserveCapacity(grid.count)
        var j = 0
        for g in grid {
            if g <= tt[0] { out.append(xx[0]); continue }
            if g >= tt[tt.count - 1] { out.append(xx[xx.count - 1]); continue }
            while j < tt.count - 2, tt[j + 1] <= g { j += 1 }
            let f = (g - tt[j]) / (tt[j + 1] - tt[j])
            out.append(xx[j] + (xx[j + 1] - xx[j]) * f)
        }
        return (grid, out)
    }
}
