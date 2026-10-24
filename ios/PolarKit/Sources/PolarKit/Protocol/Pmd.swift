// Polar Measurement Data: comandi del Control Point e frame dati (polarlab/protocol/pmd.py).
//
// Frame PMD Data:
//   [0]     tipo di misura (0x00 ECG, 0x02 ACC, ...)
//   [1..8]  timestamp uint64 LE, ns, riferito all'ULTIMO campione del frame
//   [9]     tipo di frame: bit 7 = compresso (delta), bit 0..6 = formato del campione
//   [10..]  payload

import Foundation

public struct PmdProtocolError: Error, CustomStringConvertible {
    public let description: String
    public init(_ message: String) { description = message }
}

public enum MeasurementType: UInt8, Sendable, CaseIterable {
    case ecg = 0, ppg = 1, acc = 2, ppi = 3, gyro = 5, mag = 6
    public var name: String { String(describing: self).uppercased() }
}

public enum ControlOp: UInt8 { case getSettings = 0x01, start = 0x02, stop = 0x03 }

public enum SettingType: UInt8, Comparable, CaseIterable {
    case sampleRate = 0, resolution = 1, range = 2, rangeMilliunit = 3, channels = 4, factor = 5

    public static func < (a: SettingType, b: SettingType) -> Bool { a.rawValue < b.rawValue }

    var valueSize: Int {
        switch self {
        case .sampleRate, .resolution, .range: 2
        case .rangeMilliunit, .factor: 4
        case .channels: 1
        }
    }
}

public enum PmdError: UInt8 {
    case success = 0, invalidOpCode, invalidMeasurementType, notSupported, invalidLength, invalidParameter,
         alreadyInState, invalidResolution, invalidSampleRate, invalidRange, invalidMtu,
         invalidNumberOfChannels, invalidState, deviceInCharger
}

public enum Pmd {
    public static let featuresId: UInt8 = 0x0F
    public static let controlResponseId: UInt8 = 0xF0
    public static let compressedFlag: UInt8 = 0x80
    public static let headerSize = 10

    static func channels(_ mt: MeasurementType) -> Int? {
        switch mt {
        case .ecg: 1
        case .acc, .gyro, .mag: 3
        default: nil
        }
    }

    // ------------------------------------------------------------------ comandi

    public static func parseFeatures(_ data: [UInt8]) throws -> Set<MeasurementType> {
        guard data.count >= 2, data[0] == featuresId else { throw PmdProtocolError("risposta features inattesa") }
        let mask = Int(data[1]) | (data.count > 2 ? Int(data[2]) << 8 : 0)
        return Set(MeasurementType.allCases.filter { mask & (1 << Int($0.rawValue)) != 0 })
    }

    public static func buildGetSettings(_ mt: MeasurementType) -> [UInt8] { [ControlOp.getSettings.rawValue, mt.rawValue] }
    public static func buildStop(_ mt: MeasurementType) -> [UInt8] { [ControlOp.stop.rawValue, mt.rawValue] }

    /// Comando di avvio: op, tipo, poi TLV [tipo impostazione][count=1][valore LE].
    public static func buildStart(_ mt: MeasurementType, _ settings: [SettingType: Int]) -> [UInt8] {
        var out: [UInt8] = [ControlOp.start.rawValue, mt.rawValue]
        for st in settings.keys.sorted() {
            out += [st.rawValue, 1] + le(settings[st]!, size: st.valueSize)
        }
        return out
    }

    public static func parseSettingsTLV(_ data: ArraySlice<UInt8>) throws -> [SettingType: [Double]] {
        var out: [SettingType: [Double]] = [:]
        var i = data.startIndex
        while i + 2 <= data.endIndex {
            guard let st = SettingType(rawValue: data[i]) else {
                throw PmdProtocolError(String(format: "tipo di impostazione sconosciuto 0x%02x", data[i]))
            }
            let count = Int(data[i + 1])
            let size = st.valueSize
            i += 2
            guard i + count * size <= data.endIndex else { throw PmdProtocolError("TLV troncato") }
            var values: [Double] = []
            for _ in 0..<count {
                let raw = data[i..<(i + size)].reversed().reduce(UInt32(0)) { $0 << 8 | UInt32($1) }
                values.append(st == .factor ? Double(Float(bitPattern: raw)) : Double(raw))
                i += size
            }
            out[st] = values
        }
        return out
    }

    /// Impostazioni da un comando START che abbiamo scritto (serve al decoder e al replay).
    public static func parseStartCommand(_ data: [UInt8]) throws -> (MeasurementType, [SettingType: Int])? {
        guard data.count >= 2, data[0] == ControlOp.start.rawValue,
              let mt = MeasurementType(rawValue: data[1]) else { return nil }
        let tlv = try parseSettingsTLV(data[2...])
        return (mt, tlv.mapValues { Int($0[0]) })
    }

    public struct ControlResponse {
        public var op: UInt8
        public var measType: UInt8
        public var error: PmdError
        public var moreFrames: Bool
        public var params: [UInt8]

        public var ok: Bool { error == .success || error == .alreadyInState }

        public func settings() throws -> [SettingType: [Double]] {
            op == ControlOp.getSettings.rawValue ? try parseSettingsTLV(params[...]) : [:]
        }
    }

    /// Risposta del Control Point: F0 <op> <tipo> <errore> [<more>] [parametri].
    public static func parseControlResponse(_ data: [UInt8]) throws -> ControlResponse {
        guard data.count >= 4, data[0] == controlResponseId else { throw PmdProtocolError("risposta Control Point inattesa") }
        guard let error = PmdError(rawValue: data[3]) else { throw PmdProtocolError("codice di errore sconosciuto \(data[3])") }
        return ControlResponse(op: data[1], measType: data[2], error: error, moreFrames: data.count > 4 && data[4] != 0,
                               params: data.count > 5 ? Array(data[5...]) : [])
    }

    /// Per ogni impostazione disponibile sceglie il valore più vicino a quello desiderato.
    public static func chooseSettings(_ available: [SettingType: [Double]], _ wanted: [SettingType: Int]) -> [SettingType: Int] {
        var chosen: [SettingType: Int] = [:]
        for (st, values) in available where st != .factor && !values.isEmpty {
            if let target = wanted[st] {
                chosen[st] = Int(values.min { abs($0 - Double(target)) < abs($1 - Double(target)) }!)
            } else {
                chosen[st] = Int(values[0])
            }
        }
        return chosen
    }

    public static func describe(_ s: [SettingType: [Double]]) -> String {
        let unit: [SettingType: String] = [.sampleRate: "Hz", .resolution: "bit", .range: "G"]
        return s.keys.sorted().map { k in
            s[k]!.map { String(Int($0)) }.joined(separator: "/") + " " + (unit[k] ?? "\(k)")
        }.joined(separator: ", ")
    }

    static func le(_ v: Int, size: Int) -> [UInt8] { (0..<size).map { UInt8(truncatingIfNeeded: v >> (8 * $0)) } }

    // ------------------------------------------------------------------ frame dati

    public struct StreamSettings {
        public var sampleRate: Double
        public var resolution: Int = 16
        public var range: Int?
        public var channels: Int = 1
        public var factor: Double = 1.0

        public init(sampleRate: Double, resolution: Int = 16, range: Int? = nil, channels: Int = 1, factor: Double = 1.0) {
            self.sampleRate = sampleRate
            self.resolution = resolution
            self.range = range
            self.channels = channels
            self.factor = factor
        }

        public static func from(_ mt: MeasurementType, _ s: [SettingType: Int]) -> StreamSettings {
            let d = defaultSettings[mt] ?? StreamSettings(sampleRate: 0)
            return StreamSettings(sampleRate: Double(s[.sampleRate] ?? Int(d.sampleRate)),
                                  resolution: s[.resolution] ?? d.resolution,
                                  range: s[.range],
                                  channels: s[.channels] ?? Pmd.channels(mt) ?? 1)
        }
    }

    public static let defaultSettings: [MeasurementType: StreamSettings] = [
        .ecg: StreamSettings(sampleRate: 130, resolution: 14, channels: 1),
        .acc: StreamSettings(sampleRate: 200, resolution: 16, range: 8, channels: 3),
    ]

    public struct DataFrame {
        public var measType: MeasurementType
        public var timestampNs: UInt64     // istante dell'ultimo campione, orologio della fascia
        public var frameType: Int
        public var compressed: Bool
        public var channels: Int
        public var samples: [Int]          // n × canali, per righe, nell'unità nativa (µV o mG)

        public var count: Int { samples.count / channels }
    }

    static func signed(_ v: Int64, bits: Int) -> Int64 {
        let sign = Int64(1) << (bits - 1)
        return (v ^ sign) - sign
    }

    static func decodeLE(_ p: ArraySlice<UInt8>, size: Int) -> [Int] {
        let n = p.count / size
        var out = [Int](repeating: 0, count: n)
        var i = p.startIndex
        for k in 0..<n {
            var v: Int64 = 0
            for b in 0..<size { v |= Int64(p[i + b]) << (8 * b) }
            out[k] = Int(signed(v, bits: 8 * size))
            i += size
        }
        return out
    }

    static func decodeRaw(_ p: ArraySlice<UInt8>, sampleBytes: Int, channels: Int) throws -> [Int] {
        let step = sampleBytes * channels
        guard p.count % step == 0 else { throw PmdProtocolError("payload di \(p.count) byte non multiplo di \(step)") }
        return decodeLE(p, size: sampleBytes)
    }

    /// Frame compressi: campione di riferimento + blocchi di delta impacchettati a bit (LSB first).
    static func decodeDelta(_ p: ArraySlice<UInt8>, resolution: Int, channels: Int) throws -> [Int] {
        let refSize = (resolution + 7) / 8
        let refLen = refSize * channels
        guard p.count >= refLen else { throw PmdProtocolError("frame delta senza campione di riferimento") }
        var out = decodeLE(p[p.startIndex..<(p.startIndex + refLen)], size: refSize)
        var last = out
        var i = p.startIndex + refLen
        while i + 2 <= p.endIndex {
            let bits = Int(p[i]), count = Int(p[i + 1])
            i += 2
            let nbytes = (bits * channels * count + 7) / 8
            guard i + nbytes <= p.endIndex, bits > 0 else { throw PmdProtocolError("blocco delta troncato o non valido") }
            var bitPos = 0
            for _ in 0..<count {
                for c in 0..<channels {
                    var v: Int64 = 0
                    for b in 0..<bits {
                        let byte = p[i + (bitPos >> 3)]
                        v |= Int64((byte >> UInt8(bitPos & 7)) & 1) << b
                        bitPos += 1
                    }
                    last[c] += Int(signed(v, bits: bits))
                }
                out += last
            }
            i += nbytes
        }
        return out
    }

    public static func parseDataFrame(_ data: [UInt8], _ settings: StreamSettings? = nil) throws -> DataFrame {
        guard data.count >= headerSize else { throw PmdProtocolError("frame troppo corto (\(data.count) byte)") }
        guard let mt = MeasurementType(rawValue: data[0]) else { throw PmdProtocolError("tipo di misura sconosciuto \(data[0])") }
        var ts: UInt64 = 0
        for b in 0..<8 { ts |= UInt64(data[1 + b]) << (8 * b) }
        let compressed = data[9] & compressedFlag != 0
        let ftype = Int(data[9] & 0x7F)
        let payload = data[headerSize...]
        let s = settings ?? defaultSettings[mt] ?? StreamSettings(sampleRate: 0)
        let ch = channels(mt) ?? s.channels

        var samples: [Int]
        if compressed {
            samples = try decodeDelta(payload, resolution: s.resolution, channels: ch)
        } else if mt == .ecg {
            guard ftype == 0 else { throw PmdProtocolError("frame ECG di tipo \(ftype) non supportato") }
            samples = try decodeRaw(payload, sampleBytes: 3, channels: 1)
        } else if mt == .acc || mt == .gyro || mt == .mag {
            guard (0...2).contains(ftype) else { throw PmdProtocolError("frame \(mt.name) di tipo \(ftype) non supportato") }
            samples = try decodeRaw(payload, sampleBytes: ftype + 1, channels: ch)
        } else {
            throw PmdProtocolError("decodifica di \(mt.name) non implementata")
        }
        if s.factor != 1.0 {
            samples = samples.map { Int((Double($0) * s.factor).rounded(.toNearestOrEven)) }
        }
        return DataFrame(measType: mt, timestampNs: ts, frameType: ftype, compressed: compressed, channels: ch, samples: samples)
    }

    /// Frame non compresso nello stesso formato della H10 (test e simulazioni).
    public static func encodeDataFrame(_ mt: MeasurementType, timestampNs: UInt64, samples: [Int]) -> [UInt8] {
        let ftype: UInt8 = mt == .ecg ? 0 : 1
        let size = mt == .ecg ? 3 : 2
        var out: [UInt8] = [mt.rawValue] + (0..<8).map { UInt8(truncatingIfNeeded: timestampNs >> (8 * $0)) } + [ftype]
        let lo = -(1 << (8 * size - 1)), hi = (1 << (8 * size - 1)) - 1
        for v in samples { out += le(min(max(v, lo), hi), size: size) }
        return out
    }

    // Impostazioni annunciate da una H10 reale (firmware 3.x/5.x)
    public static let h10Features: [UInt8] = [featuresId, (1 << MeasurementType.ecg.rawValue) | (1 << MeasurementType.acc.rawValue), 0]
}
