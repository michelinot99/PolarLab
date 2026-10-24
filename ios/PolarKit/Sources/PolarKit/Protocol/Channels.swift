// Pacchetto grezzo ed eventi decodificati (polarlab/protocol/channels.py, events.py).

import Foundation

public enum Channel: UInt8, Sendable {
    case hr = 1            // notifica 0x2A37
    case battery = 2       // lettura/notifica 0x2A19
    case pmdCP = 3         // risposta (indication) del PMD Control Point
    case pmdCPWrite = 4    // comando scritto da noi sul PMD Control Point
    case pmdData = 5       // notifica PMD Data (frame ECG/ACC)
}

public struct Packet: Sendable {
    public var hostT: Double      // secondi Unix all'arrivo
    public var channel: Channel
    public var data: [UInt8]

    public init(hostT: Double, channel: Channel, data: [UInt8]) {
        self.hostT = hostT
        self.channel = channel
        self.data = data
    }
}

public struct EcgChunk {
    public var t: [Double]        // s, orologio fascia
    public var uv: [Double]       // µV
    public var missingBefore: Int
}

public struct AccChunk {
    public var t: [Double]
    public var mg: [SIMD3<Double>]
    public var missingBefore: Int
}

public struct HrEvent {
    public var hostT: Double
    public var deviceT: Double    // nan se non ancora sincronizzato
    public var bpm: Int
    public var rrMs: [Double]
    public var contact: Bool?
}

public struct StreamStarted {
    public var stream: MeasurementType
    public var sampleRate: Double
    public var resolution: Int
    public var range: Int?
}

public enum DecodedEvent {
    case ecg(EcgChunk)
    case acc(AccChunk)
    case hr(HrEvent)
    case battery(Int)
    case streamStarted(StreamStarted)
}
