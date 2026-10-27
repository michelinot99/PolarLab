// Archivio delle corse: Documenti/Sessioni/<data>/ con raw.plab, meta.json e CSV.
// La cartella Documenti è visibile nell'app File (Sul mio iPhone → PolarLab), e ogni
// sessione si condivide come .zip (AirDrop verso il Mac, Mail, iCloud Drive…).

import CoreTransferable
import Foundation
import Observation
import PolarKit
import UniformTypeIdentifiers
#if os(iOS)
import UIKit
#endif

struct SessionInfo: Identifiable, Hashable {
    let id: String
    let url: URL
    var date: Date?
    var durationS: Double?
    var avgHr: Double?
    var maxHr: Double?
    var avgCadence: Double?
    var minAboveVT1: Double?
    var minAboveVT2: Double?
    var artifactPct: Double?
    var beats: Int?
    var device: String?
    var gaps: Int?
    var sizeBytes: Int64 = 0
    var finished: Bool

    var title: String {
        guard let date else { return id }
        return date.formatted(.dateTime.weekday(.wide).day().month(.wide).hour().minute())
    }
}

@MainActor
@Observable
final class SessionStore {
    let root: URL
    private(set) var sessions: [SessionInfo] = []
    private(set) var processing: Set<String> = []

    nonisolated static let folderFormat: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd_HHmmss"
        return f
    }()

    init() {
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        root = docs.appendingPathComponent("Sessioni", isDirectory: true)
        try? FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        reload()
    }

    func newSessionDir() -> URL {
        root.appendingPathComponent(Self.folderFormat.string(from: Date()), isDirectory: true)
    }

    func reload() {
        let fm = FileManager.default
        let dirs = (try? fm.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)) ?? []
        sessions = dirs.filter { fm.fileExists(atPath: $0.appendingPathComponent(SessionFormat.rawName).path) }
            .map(Self.load)
            .sorted { $0.id > $1.id }
    }

    nonisolated private static func load(_ url: URL) -> SessionInfo {
        let meta = (try? JSONSerialization.jsonObject(with: Data(contentsOf: url.appendingPathComponent(SessionFormat.metaName))))
            as? [String: Any] ?? [:]
        let s = meta["summary"] as? [String: Any] ?? [:]
        let dec = meta["decoder"] as? [String: Any] ?? [:]
        let size = ((try? FileManager.default.contentsOfDirectory(at: url, includingPropertiesForKeys: [.fileSizeKey])) ?? [])
            .reduce(Int64(0)) { $0 + Int64((try? $1.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0) }
        return SessionInfo(
            id: url.lastPathComponent, url: url, date: folderFormat.date(from: url.lastPathComponent),
            durationS: s["duration_s"] as? Double ?? meta["duration_s"] as? Double,
            avgHr: s["avg_hr"] as? Double, maxHr: s["max_hr"] as? Double, avgCadence: s["avg_cadence"] as? Double,
            minAboveVT1: s["min_above_vt1"] as? Double, minAboveVT2: s["min_above_vt2"] as? Double,
            artifactPct: s["artifact_pct"] as? Double, beats: s["beats"] as? Int, device: meta["device"] as? String,
            gaps: dec["gaps"] as? Int, sizeBytes: size, finished: meta["ended"] != nil && meta["summary"] != nil)
    }

    func delete(_ s: SessionInfo) {
        try? FileManager.default.removeItem(at: s.url)
        reload()
    }

    /// Genera CSV e riassunto in background. Su iPhone chiede a iOS qualche minuto in più
    /// se l'utente chiude l'app subito dopo la corsa.
    func process(_ dir: URL) {
        let id = dir.lastPathComponent
        guard !processing.contains(id) else { return }
        processing.insert(id)
        #if os(iOS)
        var task = UIBackgroundTaskIdentifier.invalid
        task = UIApplication.shared.beginBackgroundTask(withName: "export") { UIApplication.shared.endBackgroundTask(task) }
        #endif
        Task.detached(priority: .utility) {
            Self.finalizeMeta(dir)
            _ = try? exportSession(dir)
            await MainActor.run {
                self.processing.remove(id)
                self.reload()
                #if os(iOS)
                UIApplication.shared.endBackgroundTask(task)
                #endif
            }
        }
    }

    /// Sessioni interrotte (app chiusa da iOS, batteria scarica): si chiudono e si esportano.
    func recoverUnfinished(except active: URL?) {
        for s in sessions where !s.finished && s.url != active { process(s.url) }
    }

    nonisolated private static func finalizeMeta(_ dir: URL) {
        let url = dir.appendingPathComponent(SessionFormat.metaName)
        var meta = (try? JSONSerialization.jsonObject(with: Data(contentsOf: url))) as? [String: Any] ?? [:]
        guard meta["ended"] == nil, let packets = try? readPackets(dir), let a = packets.first, let b = packets.last else { return }
        meta["ended"] = ISO8601DateFormatter().string(from: Date(timeIntervalSince1970: b.hostT))
        meta["duration_s"] = ((b.hostT - a.hostT) * 10).rounded() / 10
        meta["packets"] = packets.count
        meta["recovered"] = true
        if let data = try? JSONSerialization.data(withJSONObject: meta, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: url, options: .atomic)
        }
    }
}

/// Una sessione come .zip, creato solo quando si condivide.
struct SessionArchive: Transferable {
    let dir: URL

    static var transferRepresentation: some TransferRepresentation {
        FileRepresentation(exportedContentType: .zip) { archive in
            SentTransferredFile(try archive.makeZip())
        }
    }

    func makeZip() throws -> URL {
        try zipFolder(dir, named: "PolarLab_\(dir.lastPathComponent).zip")
    }
}

/// Zip di una cartella nella cartella temporanea. Con .forUploading il sistema produce da solo
/// lo zip, senza librerie esterne.
func zipFolder(_ dir: URL, named name: String) throws -> URL {
    let dest = FileManager.default.temporaryDirectory.appendingPathComponent(name)
    try? FileManager.default.removeItem(at: dest)
    var coordError: NSError?
    var copyError: Error?
    NSFileCoordinator().coordinate(readingItemAt: dir, options: .forUploading, error: &coordError) { zip in
        do { try FileManager.default.copyItem(at: zip, to: dest) } catch { copyError = error }
    }
    if let e = coordError ?? copyError { throw e }
    return dest
}
