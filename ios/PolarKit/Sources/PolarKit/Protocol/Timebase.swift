// Base dei tempi: istanti dei campioni, buchi nello stream, allineamento orologi
// (polarlab/protocol/timebase.py).

import Foundation

/// Tolleranza oltre la quale un salto di timestamp non è più deriva ma perdita di campioni.
private let continuityTol = 0.03

public final class StreamClock {
    public let nominalFs: Double
    public private(set) var dt: Double
    private var prevLast: Double?

    public init(nominalFs: Double) {
        self.nominalFs = nominalFs
        dt = 1.0 / nominalFs
    }

    public var effectiveFs: Double { 1.0 / dt }

    public func reset() {
        prevLast = nil
        dt = 1.0 / nominalFs
    }

    /// (istanti dei campioni in secondi di orologio fascia, campioni mancanti prima del frame; -1 = timestamp non monotono)
    public func push(timestampNs: UInt64, n: Int) -> ([Double], Int) {
        let tLast = Double(timestampNs) * 1e-9
        var missing = 0
        if let prev = prevLast, n > 0 {
            let span = tLast - prev
            let est = span / Double(n)
            if abs(est - dt) <= continuityTol * dt {
                // Frame contiguo: aggiorna lentamente il periodo effettivo
                dt += 0.05 * (est - dt)
            } else if span > 0 {
                missing = max(0, Int((span / dt).rounded(.toNearestOrEven)) - n)
            } else {
                missing = -1
            }
        }
        prevLast = tLast
        let t = (0..<n).map { tLast - dt * Double(n - 1 - $0) }
        return (t, missing)
    }
}

/// Offset orologio telefono − orologio fascia: il minimo osservato su 60 s (stile NTP).
public final class ClockSync {
    public let windowS: Double
    private var obs: [(host: Double, off: Double)] = []
    private var head = 0
    public private(set) var offset: Double?
    public private(set) var jitterMs: Double = 0

    public init(windowS: Double = 60.0) { self.windowS = windowS }

    public func update(hostT: Double, deviceT: Double) {
        obs.append((hostT, hostT - deviceT))
        while head < obs.count, hostT - obs[head].host > windowS { head += 1 }
        if head > 512 {
            obs.removeFirst(head)
            head = 0
        }
        let offs = obs[head...].map(\.off)
        offset = offs.min()
        jitterMs = (median(offs) - offset!) * 1e3
    }

    public func toDevice(_ hostT: Double) -> Double { hostT - (offset ?? 0) }
    public func toHost(_ deviceT: Double) -> Double { deviceT + (offset ?? 0) }
}
