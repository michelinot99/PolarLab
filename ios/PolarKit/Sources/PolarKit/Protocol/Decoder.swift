// Decoder unico: Packet grezzi → eventi tipizzati (polarlab/protocol/decoder.py).

import Foundation

public struct DecoderStats {
    public var packets = 0
    public var bytes = 0
    public var ecgFrames = 0
    public var accFrames = 0
    public var hrPackets = 0
    public var gaps = 0
    public var missingSamples = 0
    public var errors = 0
    public var lastError = ""
}

public final class StreamDecoder {
    public private(set) var settings: [MeasurementType: Pmd.StreamSettings] = [:]
    private var startCmds: [MeasurementType: [UInt8]] = [:]
    public private(set) var clocks: [MeasurementType: StreamClock] = [:]
    public let sync = ClockSync()
    public private(set) var stats = DecoderStats()
    public private(set) var features: Set<MeasurementType> = []

    public init() {}

    public func feed(_ p: Packet) -> [DecodedEvent] {
        stats.packets += 1
        stats.bytes += p.data.count
        do {
            return try dispatch(p)
        } catch {
            stats.errors += 1
            stats.lastError = "\(p.channel): \(error)"
            return []
        }
    }

    private func dispatch(_ p: Packet) throws -> [DecodedEvent] {
        switch p.channel {
        case .pmdData:
            return try onData(p)
        case .hr:
            let m = try parseHrMeasurement(p.data)
            stats.hrPackets += 1
            // Senza frame PMD non c'è ancora un orologio comune: istante sconosciuto
            let dev = sync.offset != nil ? sync.toDevice(p.hostT) : .nan
            return [.hr(HrEvent(hostT: p.hostT, deviceT: dev, bpm: m.bpm, rrMs: m.rrMs, contact: m.contact))]
        case .battery:
            return p.data.isEmpty ? [] : [.battery(Int(p.data[0]))]
        case .pmdCPWrite:
            return try onCommand(p.data)
        case .pmdCP:
            if p.data.first == Pmd.featuresId { features = try Pmd.parseFeatures(p.data) }
            return []
        }
    }

    private func onCommand(_ data: [UInt8]) throws -> [DecodedEvent] {
        guard let (mt, s) = try Pmd.parseStartCommand(data) else { return [] }
        let st = Pmd.StreamSettings.from(mt, s)
        settings[mt] = st
        startCmds[mt] = data
        // Riavvio dello stream (riconnessione, STOP+START): l'orologio della fascia continua,
        // quindi si tiene il vecchio StreamClock e il buco emerge dai timestamp. Con un orologio
        // nuovo il primo frame sembrerebbe contiguo all'ultimo e l'RR a cavallo del buco
        // (anche diversi secondi) entrerebbe nelle metriche come un battito vero.
        if clocks[mt]?.nominalFs != st.sampleRate {
            clocks[mt] = StreamClock(nominalFs: st.sampleRate)
        }
        return [.streamStarted(StreamStarted(stream: mt, sampleRate: st.sampleRate, resolution: st.resolution, range: st.range))]
    }

    private func onData(_ p: Packet) throws -> [DecodedEvent] {
        guard let first = p.data.first, let mt = MeasurementType(rawValue: first) else {
            throw PmdProtocolError("tipo di misura sconosciuto")
        }
        let st = settings[mt] ?? Pmd.defaultSettings[mt] ?? Pmd.StreamSettings(sampleRate: 0)
        let frame = try Pmd.parseDataFrame(p.data, st)
        let n = frame.count
        if n == 0 { return [] }
        let clock: StreamClock
        if let c = clocks[mt] {
            clock = c
        } else {
            clock = StreamClock(nominalFs: st.sampleRate)
            clocks[mt] = clock
        }
        let (t, missing) = clock.push(timestampNs: frame.timestampNs, n: n)
        sync.update(hostT: p.hostT, deviceT: t[n - 1])
        if missing > 0 {
            stats.gaps += 1
            stats.missingSamples += missing
        }
        switch mt {
        case .ecg:
            stats.ecgFrames += 1
            return [.ecg(EcgChunk(t: t, uv: frame.samples.map(Double.init), missingBefore: missing))]
        case .acc:
            stats.accFrames += 1
            let s = frame.samples
            let mg = (0..<n).map { SIMD3(Double(s[3 * $0]), Double(s[3 * $0 + 1]), Double(s[3 * $0 + 2])) }
            return [.acc(AccChunk(t: t, mg: mg, missingBefore: missing))]
        default:
            return []
        }
    }

    /// Comandi START già visti: aprono ogni registrazione, così il replay è autosufficiente.
    public func configPackets(hostT: Double) -> [Packet] {
        startCmds.keys.sorted { $0.rawValue < $1.rawValue }.map { Packet(hostT: hostT, channel: .pmdCPWrite, data: startCmds[$0]!) }
    }

    public func sampleRate(_ mt: MeasurementType) -> Double {
        if let c = clocks[mt] { return c.effectiveFs }
        return (settings[mt] ?? Pmd.defaultSettings[mt])?.sampleRate ?? 0
    }
}
