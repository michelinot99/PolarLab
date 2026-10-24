// Registrazione delle sessioni (polarlab/core/session.py).
//
// raw.plab, stesso formato del Mac:
//   intestazione  "PLAB\x01\n"
//   record        <f8 host_t> <u1 canale> <u2 lunghezza> <byte...>   (little-endian)
// Le CSV si rigenerano dal log grezzo, sul telefono o sul Mac.

import Foundation

public enum SessionFormat {
    public static let magic: [UInt8] = Array("PLAB\u{01}\n".utf8)
    public static let rawName = "raw.plab"
    public static let metaName = "meta.json"
}

/// Scrive raw.plab a blocchi: i pacchetti restano in memoria e vanno su disco ogni
/// `flushInterval` secondi (meno risvegli del disco = meno batteria). Se l'app viene
/// chiusa di colpo si perdono al massimo gli ultimi secondi: il lettore tollera un
/// record troncato.
public final class SessionWriter {
    public let dir: URL
    public private(set) var packets = 0
    public private(set) var firstHostT: Double?
    public private(set) var lastHostT: Double?
    public var meta: [String: Any]
    private let handle: FileHandle
    private var buffer = Data()
    private var lastFlush = Date()
    let flushInterval: TimeInterval

    /// Con `resume` si continua una sessione esistente (app rilanciata da iOS durante la corsa).
    public init(dir: URL, meta: [String: Any], resume: Bool = false, flushInterval: TimeInterval = 10) throws {
        self.dir = dir
        self.flushInterval = flushInterval
        let fm = FileManager.default
        try fm.createDirectory(at: dir, withIntermediateDirectories: true)
        let raw = dir.appendingPathComponent(SessionFormat.rawName)
        let metaURL = dir.appendingPathComponent(SessionFormat.metaName)
        var m = meta
        if resume, fm.fileExists(atPath: raw.path),
           let old = (try? JSONSerialization.jsonObject(with: Data(contentsOf: metaURL))) as? [String: Any] {
            m = old.merging(meta) { _, new in new }
            m["started"] = old["started"]
            m["resumed"] = ((old["resumed"] as? Int) ?? 0) + 1
            m["ended"] = nil
        } else {
            fm.createFile(atPath: raw.path, contents: Data(SessionFormat.magic))
            m["started"] = isoNow()
        }
        if let first = try? readPackets(raw).first { firstHostT = first.hostT }
        handle = try FileHandle(forWritingTo: raw)
        handle.seekToEndOfFile()
        m["app"] = "PolarLab iOS"
        self.meta = m
        try writeMeta()
    }

    public func write(_ p: Packet) {
        var t = p.hostT.bitPattern.littleEndian
        withUnsafeBytes(of: &t) { buffer.append(contentsOf: $0) }
        buffer.append(p.channel.rawValue)
        var n = UInt16(p.data.count).littleEndian
        withUnsafeBytes(of: &n) { buffer.append(contentsOf: $0) }
        buffer.append(contentsOf: p.data)
        packets += 1
        if firstHostT == nil { firstHostT = p.hostT }
        lastHostT = p.hostT
        if Date().timeIntervalSince(lastFlush) >= flushInterval { flush() }
    }

    public func flush() {
        if !buffer.isEmpty {
            handle.write(buffer)
            buffer.removeAll(keepingCapacity: true)
        }
        lastFlush = Date()
    }

    @discardableResult
    public func close(extra: [String: Any] = [:]) throws -> URL {
        flush()
        try handle.close()
        meta["ended"] = isoNow()
        meta["duration_s"] = (((lastHostT ?? 0) - (firstHostT ?? 0)) * 10).rounded() / 10
        meta["packets"] = packets
        for (k, v) in extra { meta[k] = v }
        try writeMeta()
        return dir
    }

    private func writeMeta() throws {
        let data = try JSONSerialization.data(withJSONObject: meta, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: dir.appendingPathComponent(SessionFormat.metaName), options: .atomic)
    }
}

func isoNow() -> String {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
    return f.string(from: Date())
}

public func readPackets(_ url: URL) throws -> [Packet] {
    let file = url.hasDirectoryPath || url.pathExtension.isEmpty ? url.appendingPathComponent(SessionFormat.rawName) : url
    let d = [UInt8](try Data(contentsOf: file))
    guard d.count >= SessionFormat.magic.count, Array(d.prefix(SessionFormat.magic.count)) == SessionFormat.magic else {
        throw PmdProtocolError("\(file.lastPathComponent) non è un file PolarLab")
    }
    var out: [Packet] = []
    var i = SessionFormat.magic.count
    while i + 11 <= d.count {
        var bits: UInt64 = 0
        for b in 0..<8 { bits |= UInt64(d[i + b]) << (8 * b) }
        let ch = d[i + 8]
        let n = Int(d[i + 9]) | Int(d[i + 10]) << 8
        i += 11
        if i + n > d.count { break }   // registrazione interrotta a metà record
        if let c = Channel(rawValue: ch) {
            out.append(Packet(hostT: Double(bitPattern: bits), channel: c, data: Array(d[i..<(i + n)])))
        }
        i += n
    }
    return out
}

/// Riassunto di una corsa per l'elenco delle sessioni (salvato in meta.json → "summary").
public struct SessionSummary: Codable {
    public var durationS: Double
    public var avgHr: Double?
    public var maxHr: Double?
    public var avgCadence: Double?
    public var beats: Int
    public var artifactPct: Double?
    public var minutesAboveVT1: Double?    // α1 < 0.75
    public var minutesAboveVT2: Double?    // α1 < 0.50

    public var dictionary: [String: Any] {
        var d: [String: Any] = ["duration_s": durationS, "beats": beats]
        if let avgHr { d["avg_hr"] = avgHr }
        if let maxHr { d["max_hr"] = maxHr }
        if let avgCadence { d["avg_cadence"] = avgCadence }
        if let artifactPct { d["artifact_pct"] = artifactPct }
        if let minutesAboveVT1 { d["min_above_vt1"] = minutesAboveVT1 }
        if let minutesAboveVT2 { d["min_above_vt2"] = minutesAboveVT2 }
        return d
    }
}

/// Rielabora raw.plab e scrive ecg.csv, acc.csv, hr.csv, beats.csv, metrics.csv
/// (come `export_session` su Mac). Ritorna il riassunto della corsa.
@discardableResult
public func exportSession(_ dir: URL, progress: ((Double) -> Void)? = nil) throws -> SessionSummary {
    let packets = try readPackets(dir)
    let dec = StreamDecoder()
    let eng = Engine(keepLog: true)
    var ecg = "t_s,ecg_uv\n", accCsv = "t_s,x_mg,y_mg,z_mg\n", hr = "t_s,hr_bpm,rr_ms,contact\n"
    var ecgRows: [(Double, Double)] = [], accRows: [(Double, SIMD3<Double>)] = []
    var hrRows: [(Double, Int, [Double], Bool?)] = []
    var cadences: [Double] = []
    var a1: [Double] = []
    eng.onTick = { m, _ in
        if m.cadenceSpm.isFinite { cadences.append(m.cadenceSpm) }
        a1.append(m.dfaA1)
    }
    for (k, p) in packets.enumerated() {
        for ev in dec.feed(p) {
            eng.handle(ev)
            switch ev {
            case .ecg(let c): for (t, v) in zip(c.t, c.uv) { ecgRows.append((t, v)) }
            case .acc(let c): for (t, v) in zip(c.t, c.mg) { accRows.append((t, v)) }
            case .hr(let h): hrRows.append((h.deviceT, h.bpm, h.rrMs, h.contact))
            default: break
            }
        }
        if k % 2000 == 0 { progress?(Double(k) / Double(max(1, packets.count))) }
    }
    eng.finish()
    let t0 = eng.t0 ?? 0
    func f(_ v: Double, _ d: Int) -> String { String(format: "%.\(d)f", v) }

    ecg.reserveCapacity(ecgRows.count * 16)
    for (t, v) in ecgRows { ecg += "\(f(t - t0, 4)),\(Int(v))\n" }
    for (t, v) in accRows { accCsv += "\(f(t - t0, 4)),\(Int(v.x)),\(Int(v.y)),\(Int(v.z))\n" }
    for (t, b, rr, c) in hrRows {
        let contact = c.map { $0 ? "True" : "False" } ?? ""
        hr += "\(t.isFinite ? f(t - t0, 3) : "nan"),\(b),\(rr.map { f($0, 1) }.joined(separator: ";")),\(contact)\n"
    }
    var beats = "t,rr_ms,nn_ms,kind\n"
    for r in eng.beatLog { beats += "\(f(r.t, 4)),\(f(r.rrMs, 2)),\(f(r.nnMs, 2)),\(r.kind.rawValue)\n" }
    let metrics = ([Metrics.csvHeader] + eng.metricsLog).map { $0.joined(separator: ",") }.joined(separator: "\n") + "\n"

    for (name, text) in [("ecg.csv", ecg), ("acc.csv", accCsv), ("hr.csv", hr), ("beats.csv", beats), ("metrics.csv", metrics)] {
        try text.write(to: dir.appendingPathComponent(name), atomically: true, encoding: .utf8)
    }

    // Riassunto
    let nn = eng.beatLog.filter { $0.kind != .invalid }
    let hrSeries = eng.metricsLog.compactMap { Double($0[1]) }
    let ok = eng.beatLog.filter { $0.kind == .normal }.count
    let validA1 = a1.filter(\.isFinite)
    let summary = SessionSummary(
        durationS: ((packets.last?.hostT ?? 0) - (packets.first?.hostT ?? 0)),
        avgHr: nn.isEmpty ? nil : 60000 / mean(nn.map(\.nnMs)),
        maxHr: hrSeries.max(),
        avgCadence: cadences.isEmpty ? nil : mean(cadences),
        beats: eng.beatLog.count,
        artifactPct: eng.beatLog.isEmpty ? nil : 100 * Double(eng.beatLog.count - ok) / Double(eng.beatLog.count),
        minutesAboveVT1: validA1.isEmpty ? nil : Double(validA1.filter { $0 < aetAlpha1 }.count) / 60,
        minutesAboveVT2: validA1.isEmpty ? nil : Double(validA1.filter { $0 < antAlpha1 }.count) / 60)

    let metaURL = dir.appendingPathComponent(SessionFormat.metaName)
    var meta = (try? JSONSerialization.jsonObject(with: Data(contentsOf: metaURL))) as? [String: Any] ?? [:]
    let st = dec.stats
    meta["decoder"] = ["packets": st.packets, "gaps": st.gaps, "missing_samples": st.missingSamples, "errors": st.errors]
    meta["summary"] = summary.dictionary
    if meta["duration_s"] == nil { meta["duration_s"] = (summary.durationS * 10).rounded() / 10 }
    let data = try JSONSerialization.data(withJSONObject: meta, options: [.prettyPrinted, .sortedKeys])
    try data.write(to: metaURL, options: .atomic)
    progress?(1)
    return summary
}
