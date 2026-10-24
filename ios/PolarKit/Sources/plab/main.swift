// plab: strumento per il Mac che esercita PolarKit senza Xcode.
//
//   plab selftest                     vettori di byte del protocollo (come tests/test_pmd.py)
//   plab analyze <sessione> <uscita>  rielabora raw.plab e scrive le CSV in <uscita>
//   plab dsp                          stampa in JSON i risultati delle primitive numeriche,
//                                     da confrontare con scipy (scripts/compare_swift.py)

import Foundation
import PolarKit

var failures = 0

func check(_ cond: Bool, _ what: String) {
    if !cond {
        failures += 1
        print("✗ \(what)")
    }
}

func hex(_ s: String) -> [UInt8] {
    let c = Array(s.replacingOccurrences(of: " ", with: ""))
    return stride(from: 0, to: c.count, by: 2).map { UInt8(String(c[$0...$0 + 1]), radix: 16)! }
}

func selftest() throws {
    check(Pmd.buildStart(.ecg, [.sampleRate: 130, .resolution: 14]) == hex("02 00 00 01 82 00 01 01 0e 00"), "START ECG")
    check(Pmd.buildStart(.acc, [.range: 8, .sampleRate: 200, .resolution: 16]) == hex("02 02 00 01 c8 00 01 01 10 00 02 01 08 00"),
          "START ACC")
    check(Pmd.buildGetSettings(.acc) == [0x01, 0x02] && Pmd.buildStop(.ecg) == [0x03, 0x00], "GET_SETTINGS / STOP")
    let cmd = Pmd.buildStart(.acc, [.sampleRate: 100, .resolution: 16, .range: 4])
    let parsed = try Pmd.parseStartCommand(cmd)
    check(parsed?.0 == .acc && parsed?.1 == [.sampleRate: 100, .resolution: 16, .range: 4], "parse START")
    check(try Pmd.parseFeatures(hex("0f0500")) == [.ecg, .acc], "features")

    let r = try Pmd.parseControlResponse(hex("f0010000000001820001010e00"))
    check(r.ok && r.op == 1 && r.measType == 0, "risposta GET_SETTINGS")
    check(try r.settings() == [.sampleRate: [130], .resolution: [14]], "impostazioni ECG")
    let e1 = try Pmd.parseControlResponse(hex("f002000600"))
    check(e1.error == .alreadyInState && e1.ok, "ALREADY_IN_STATE")
    let e2 = try Pmd.parseControlResponse(hex("f002020800"))
    check(e2.error == .invalidSampleRate && !e2.ok, "INVALID_SAMPLE_RATE")
    let chosen = Pmd.chooseSettings([.sampleRate: [25, 50, 100, 200], .resolution: [16], .range: [2, 4, 8]],
                                    [.sampleRate: 120, .range: 16])
    check(chosen == [.sampleRate: 100, .resolution: 16, .range: 8], "scelta impostazioni")

    let ts: UInt64 = 599_000_000_123_456_789
    let raw: [UInt8] = [0x00] + (0..<8).map { UInt8(truncatingIfNeeded: ts >> (8 * $0)) } + [0x00] + hex("ffffff e80300 30f8ff ffff7f 000080")
    let f = try Pmd.parseDataFrame(raw)
    check(f.measType == .ecg && f.timestampNs == ts && !f.compressed, "intestazione frame ECG")
    check(f.samples == [-1, 1000, -2000, 8_388_607, -8_388_608], "campioni ECG int24")

    let xyz = [-1000, 2, 3, 4, -5, 32767, -32768, 0, 15]
    let accRaw = Pmd.encodeDataFrame(.acc, timestampNs: 42, samples: xyz)
    check(accRaw[9] == 0x01 && accRaw.count == 10 + 18, "frame ACC int16")
    check(try Pmd.parseDataFrame(accRaw).samples == xyz, "campioni ACC")

    // Frame delta: 3 canali, risoluzione 16, blocco di 2 campioni a 4 bit
    // riferimento (1000, -1, 7), poi delta (+1,-2,+3) e (-4,0,+7)
    var delta: [UInt8] = [0x02] + Array(repeating: 0, count: 8) + [0x81]
    delta += [0xE8, 0x03, 0xFF, 0xFF, 0x07, 0x00, 4, 2]
    let nibbles: [Int] = [1, -2, 3, -4, 0, 7]
    for i in stride(from: 0, to: 6, by: 2) { delta.append(UInt8((nibbles[i] & 0xF) | (nibbles[i + 1] & 0xF) << 4)) }
    let df = try Pmd.parseDataFrame(delta, Pmd.StreamSettings(sampleRate: 50, resolution: 16, channels: 3))
    check(df.samples == [1000, -1, 7, 1001, -3, 10, 997, -3, 17], "frame delta")

    do {
        _ = try Pmd.parseDataFrame([0x00, 0x01])
        check(false, "frame corto accettato")
    } catch {}
    do {
        _ = try Pmd.parseDataFrame(Array(repeating: 0, count: 10) + [0x01, 0x02])
        check(false, "ECG non multiplo di 3 accettato")
    } catch {}

    let m = try parseHrMeasurement(hex("163c0004"))
    check(m.bpm == 60 && m.contact == true && m.rrMs == [1000.0] && m.energyKj == nil, "HR tipico H10")
    let m2 = try parseHrMeasurement([0x19, 0x2C, 0x01, 12, 0, 0x00, 0x02, 0x00, 0x01])
    check(m2.bpm == 300 && m2.energyKj == 12 && m2.rrMs == [500, 250] && m2.contact == nil, "HR uint16 + energia")
    check(try parseHrMeasurement([0x04, 70]).contact == false, "contatto assente")

    let c = StreamClock(nominalFs: 130)
    let (t, miss) = c.push(timestampNs: 10_000_000_000, n: 73)
    check(miss == 0 && abs(t[72] - 10) < 1e-12 && abs(t[1] - t[0] - 1 / 130.0) < 1e-12, "istanti dei campioni")
    _ = c.push(timestampNs: UInt64((10 + 73 / 130.0) * 1e9), n: 73)
    let (_, lost) = c.push(timestampNs: UInt64((10 + 4 * 73 / 130.0) * 1e9), n: 73)
    check(lost == 146, "frame persi (\(lost))")
    let (_, back) = c.push(timestampNs: 1_000_000_000, n: 73)
    check(back == -1, "timestamp non monotono")

    // Sessione interrotta e ripresa: un solo raw.plab leggibile per intero
    let dir = FileManager.default.temporaryDirectory.appendingPathComponent("plab-resume-\(UUID().uuidString)")
    defer { try? FileManager.default.removeItem(at: dir) }
    let w1 = try SessionWriter(dir: dir, meta: ["device": "test"])
    for k in 0..<5 { w1.write(Packet(hostT: 100 + Double(k), channel: .battery, data: [UInt8(k)])) }
    w1.flush()   // come se iOS terminasse l'app: niente close()
    let w2 = try SessionWriter(dir: dir, meta: ["device": "test"], resume: true)
    for k in 5..<8 { w2.write(Packet(hostT: 100 + Double(k), channel: .battery, data: [UInt8(k)])) }
    try w2.close()
    let readBack = try readPackets(dir)
    check(readBack.map { Int($0.data[0]) } == Array(0..<8), "ripresa sessione: pacchetti")
    let meta = try JSONSerialization.jsonObject(with: Data(contentsOf: dir.appendingPathComponent("meta.json"))) as? [String: Any]
    check(meta?["duration_s"] as? Double == 7 && meta?["resumed"] as? Int == 1, "ripresa sessione: meta \(meta ?? [:])")

    print(failures == 0 ? "selftest: tutto ok" : "selftest: \(failures) errori")
}

func analyze(_ src: URL, _ out: URL) throws {
    let fm = FileManager.default
    try fm.createDirectory(at: out, withIntermediateDirectories: true)
    for name in [SessionFormat.rawName, SessionFormat.metaName] {
        let d = out.appendingPathComponent(name)
        try? fm.removeItem(at: d)
        if fm.fileExists(atPath: src.appendingPathComponent(name).path) {
            try fm.copyItem(at: src.appendingPathComponent(name), to: d)
        }
    }
    let t = Date()
    let s = try exportSession(out)
    print(String(format: "analisi in %.2f s · %d battiti · FC media %.1f · durata %.0f s",
                 Date().timeIntervalSince(t), s.beats, s.avgHr ?? .nan, s.durationS))
}

let args = CommandLine.arguments
do {
    switch args.count > 1 ? args[1] : "" {
    case "selftest":
        try selftest()
    case "analyze" where args.count == 4:
        try analyze(URL(fileURLWithPath: args[2]), URL(fileURLWithPath: args[3]))
    case "dsp":
        print(dspReport())
    default:
        print("uso: plab selftest | plab analyze <sessione> <uscita> | plab dsp")
    }
} catch {
    print("errore: \(error)")
    exit(1)
}
exit(failures == 0 ? 0 : 1)
