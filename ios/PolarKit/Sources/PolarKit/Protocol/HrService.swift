// Heart Rate Measurement 0x2A37 (polarlab/protocol/hr_service.py).

import Foundation

public let rrUnitMs = 1000.0 / 1024.0

public struct HrMeasurement: Equatable {
    public var bpm: Int
    public var rrMs: [Double]
    public var contact: Bool?        // nil = la fascia non riporta il contatto
    public var energyKj: Int?
}

public func parseHrMeasurement(_ data: [UInt8]) throws -> HrMeasurement {
    guard data.count >= 2 else { throw PmdProtocolError("misura HR troppo corta") }
    let flags = data[0]
    var i = 1
    let bpm: Int
    if flags & 0x01 != 0 {
        guard data.count >= 3 else { throw PmdProtocolError("misura HR troppo corta") }
        bpm = Int(data[1]) | Int(data[2]) << 8
        i += 2
    } else {
        bpm = Int(data[1])
        i += 1
    }
    let contact: Bool? = flags & 0x04 != 0 ? (flags & 0x02 != 0) : nil
    var energy: Int?
    if flags & 0x08 != 0, i + 2 <= data.count {
        energy = Int(data[i]) | Int(data[i + 1]) << 8
        i += 2
    }
    var rr: [Double] = []
    if flags & 0x10 != 0 {
        while i + 2 <= data.count {
            rr.append(Double(Int(data[i]) | Int(data[i + 1]) << 8) * rrUnitMs)
            i += 2
        }
    }
    return HrMeasurement(bpm: bpm, rrMs: rr, contact: contact, energyKj: energy)
}
