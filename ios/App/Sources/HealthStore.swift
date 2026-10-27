// Allenamenti di Apple Health: elenco e export di un allenamento in una cartella con
// workout.json, heart_rate.csv, series.csv e route.gpx, da condividere al Mac come .zip.
// Solo lettura: PolarLab non scrive nulla in Salute.

import CoreLocation
import Foundation
import HealthKit
import Observation

let healthKit = HKHealthStore()

/// Una grandezza di Salute da esportare, con l'unità usata nei file.
struct HealthMetric {
    let id: HKQuantityTypeIdentifier
    let key: String
    let unit: HKUnit
    let unitName: String

    var type: HKQuantityType { HKQuantityType(id) }

    static let heartRate = HealthMetric(id: .heartRate, key: "heart_rate", unit: .count().unitDivided(by: .minute()), unitName: "bpm")
    /// Tutto tranne la FC, che ha un file suo.
    static let series: [HealthMetric] = [
        HealthMetric(id: .distanceWalkingRunning, key: "distance_walking_running", unit: .meter(), unitName: "m"),
        HealthMetric(id: .distanceCycling, key: "distance_cycling", unit: .meter(), unitName: "m"),
        HealthMetric(id: .distanceSwimming, key: "distance_swimming", unit: .meter(), unitName: "m"),
        HealthMetric(id: .stepCount, key: "steps", unit: .count(), unitName: "count"),
        HealthMetric(id: .activeEnergyBurned, key: "active_energy", unit: .kilocalorie(), unitName: "kcal"),
        HealthMetric(id: .runningSpeed, key: "running_speed", unit: .meter().unitDivided(by: .second()), unitName: "m/s"),
        HealthMetric(id: .runningPower, key: "running_power", unit: .watt(), unitName: "W"),
        HealthMetric(id: .runningStrideLength, key: "stride_length", unit: .meter(), unitName: "m"),
        HealthMetric(id: .runningVerticalOscillation, key: "vertical_oscillation", unit: .meterUnit(with: .centi), unitName: "cm"),
        HealthMetric(id: .runningGroundContactTime, key: "ground_contact_time", unit: .secondUnit(with: .milli), unitName: "ms"),
        HealthMetric(id: .cyclingSpeed, key: "cycling_speed", unit: .meter().unitDivided(by: .second()), unitName: "m/s"),
        HealthMetric(id: .cyclingPower, key: "cycling_power", unit: .watt(), unitName: "W"),
        HealthMetric(id: .cyclingCadence, key: "cycling_cadence", unit: .count().unitDivided(by: .minute()), unitName: "rpm"),
        HealthMetric(id: .swimmingStrokeCount, key: "swimming_strokes", unit: .count(), unitName: "count"),
    ]
    static let all = [heartRate] + series
}

@MainActor
@Observable
final class HealthStore {
    static let pageDays = 90

    private(set) var workouts: [HKWorkout] = []
    private(set) var days = pageDays
    private(set) var loading = false
    private(set) var error: String?

    var available: Bool { HKHealthStore.isHealthDataAvailable() }

    /// Chiede il permesso (solo la prima volta iOS mostra il foglio) e legge gli allenamenti
    /// degli ultimi `days` giorni. Se la lettura è negata, iOS restituisce semplicemente un elenco vuoto.
    func load(more: Bool = false) async {
        guard available, !loading else { return }
        if more { days += Self.pageDays }
        loading = true
        defer { loading = false }
        do {
            var read: Set<HKObjectType> = [HKObjectType.workoutType(), HKSeriesType.workoutRoute()]
            for m in HealthMetric.all { read.insert(m.type) }
            try await healthKit.requestAuthorization(toShare: [], read: read)
            let since = Calendar.current.date(byAdding: .day, value: -days, to: Date())
            let query = HKSampleQueryDescriptor(
                predicates: [.workout(HKQuery.predicateForSamples(withStart: since, end: nil))],
                sortDescriptors: [SortDescriptor(\.startDate, order: .reverse)])
            workouts = try await query.result(for: healthKit)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}

// MARK: - Nomi

extension HKWorkoutActivityType {
    /// Nome per i file (inglese), nome per l'interfaccia, simbolo SF.
    var kind: (slug: String, name: String, symbol: String) {
        switch self {
        case .running: ("running", "Corsa", "figure.run")
        case .walking: ("walking", "Camminata", "figure.walk")
        case .hiking: ("hiking", "Escursione", "figure.hiking")
        case .cycling: ("cycling", "Ciclismo", "figure.outdoor.cycle")
        case .swimming: ("swimming", "Nuoto", "figure.pool.swim")
        case .rowing: ("rowing", "Canottaggio", "figure.rower")
        case .elliptical: ("elliptical", "Ellittica", "figure.elliptical")
        case .stairClimbing: ("stair_climbing", "Scale", "figure.stair.stepper")
        case .highIntensityIntervalTraining: ("hiit", "HIIT", "figure.highintensity.intervaltraining")
        case .traditionalStrengthTraining, .functionalStrengthTraining: ("strength", "Forza", "figure.strengthtraining.traditional")
        case .coreTraining: ("core", "Core", "figure.core.training")
        case .yoga: ("yoga", "Yoga", "figure.yoga")
        case .crossTraining: ("cross_training", "Cross training", "figure.cross.training")
        case .mixedCardio: ("mixed_cardio", "Cardio misto", "figure.mixed.cardio")
        default: ("other_\(rawValue)", "Allenamento", "figure.mixed.cardio")
        }
    }
}

extension HKWorkout {
    var isIndoor: Bool { (metadata?[HKMetadataKeyIndoorWorkout] as? NSNumber)?.boolValue ?? false }

    var title: String { workoutActivityType.kind.name + (isIndoor ? " al chiuso" : "") }

    var avgHr: Double? { stat(HealthMetric.heartRate)?.averageQuantity()?.doubleValue(for: HealthMetric.heartRate.unit) }
    var maxHr: Double? { stat(HealthMetric.heartRate)?.maximumQuantity()?.doubleValue(for: HealthMetric.heartRate.unit) }
    var activeKcal: Double? { statistics(for: HKQuantityType(.activeEnergyBurned))?.sumQuantity()?.doubleValue(for: .kilocalorie()) }
    var distanceM: Double? {
        [HKQuantityTypeIdentifier.distanceWalkingRunning, .distanceCycling, .distanceSwimming].lazy
            .compactMap { self.statistics(for: HKQuantityType($0))?.sumQuantity()?.doubleValue(for: .meter()) }
            .first
    }

    private func stat(_ m: HealthMetric) -> HKStatistics? { statistics(for: m.type) }
}

// MARK: - Export

private struct SeriesPoint {
    let start: Date
    let end: Date
    let value: Double
    let source: String
}

/// Raccoglie i risultati delle query a callback di HealthKit (chiamate in serie sulla sua coda)
/// e riprende la continuation una volta sola.
private final class Collector<T>: @unchecked Sendable {
    var items: [T] = []
    private var resumed = false
    func once(_ f: () -> Void) {
        guard !resumed else { return }
        resumed = true
        f()
    }
}

private func isNoData(_ error: Error) -> Bool {
    (error as? HKError)?.code == .errorNoData
}

/// Tutti i valori di una grandezza durante l'allenamento, uno per misura (non le medie per
/// campione: per la FC del Watch un campione contiene molte misure).
private func quantitySeries(_ m: HealthMetric, in w: HKWorkout) async throws -> [SeriesPoint] {
    let pred = HKQuery.predicateForSamples(withStart: w.startDate, end: w.endDate, options: [])
    return try await withCheckedThrowingContinuation { cont in
        let c = Collector<SeriesPoint>()
        let q = HKQuantitySeriesSampleQuery(quantityType: m.type, predicate: pred) { _, quantity, interval, sample, done, error in
            if let error {
                c.once { isNoData(error) ? cont.resume(returning: c.items) : cont.resume(throwing: error) }
                return
            }
            if let quantity, let interval {
                c.items.append(SeriesPoint(start: interval.start, end: interval.end, value: quantity.doubleValue(for: m.unit),
                                           source: sample?.sourceRevision.source.name ?? ""))
            }
            if done { c.once { cont.resume(returning: c.items) } }
        }
        q.includeSample = true
        q.orderByQuantitySampleStartDate = true
        healthKit.execute(q)
    }
}

/// Le tracce GPS dell'allenamento (di solito una; più di una se ci sono state interruzioni).
private func routes(of w: HKWorkout) async throws -> [[CLLocation]] {
    let query = HKSampleQueryDescriptor(
        predicates: [.sample(type: HKSeriesType.workoutRoute(), predicate: HKQuery.predicateForObjects(from: w))],
        sortDescriptors: [SortDescriptor(\.startDate)])
    var out: [[CLLocation]] = []
    for case let route as HKWorkoutRoute in try await query.result(for: healthKit) {
        let locs: [CLLocation] = try await withCheckedThrowingContinuation { cont in
            let c = Collector<CLLocation>()
            let q = HKWorkoutRouteQuery(route: route) { _, locations, done, error in
                if let error {
                    c.once { isNoData(error) ? cont.resume(returning: c.items) : cont.resume(throwing: error) }
                    return
                }
                c.items += locations ?? []
                if done { c.once { cont.resume(returning: c.items) } }
            }
            healthKit.execute(q)
        }
        if !locs.isEmpty { out.append(locs) }
    }
    return out
}

/// Scrive l'allenamento in una cartella temporanea `Salute_<data>_<tipo>` e la restituisce.
func exportWorkout(_ w: HKWorkout) async throws -> URL {
    let iso = ISO8601DateFormatter()
    iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let t0 = w.startDate
    let name = "Salute_\(SessionStore.folderFormat.string(from: t0))_\(w.workoutActivityType.kind.slug)"
    let dir = FileManager.default.temporaryDirectory.appendingPathComponent(name, isDirectory: true)
    try? FileManager.default.removeItem(at: dir)
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    var files: [String] = []

    let hr = try await quantitySeries(.heartRate, in: w)
    if !hr.isEmpty {
        var csv = "time_iso,t_s,bpm,source\n"
        for p in hr {
            csv += "\(iso.string(from: p.start)),\(num(p.start.timeIntervalSince(t0), 3)),\(num(p.value, 1)),\(field(p.source))\n"
        }
        try csv.write(to: dir.appendingPathComponent("heart_rate.csv"), atomically: true, encoding: .utf8)
        files.append("heart_rate.csv")
    }

    var csv = "metric,start_iso,end_iso,t_s,dur_s,value,unit,source\n"
    var rows = 0
    for m in HealthMetric.series {
        for p in try await quantitySeries(m, in: w) {
            csv += "\(m.key),\(iso.string(from: p.start)),\(iso.string(from: p.end)),\(num(p.start.timeIntervalSince(t0), 3)),"
                + "\(num(p.end.timeIntervalSince(p.start), 3)),\(num(p.value, 4)),\(m.unitName),\(field(p.source))\n"
            rows += 1
        }
    }
    if rows > 0 {
        try csv.write(to: dir.appendingPathComponent("series.csv"), atomically: true, encoding: .utf8)
        files.append("series.csv")
    }

    let tracks = try await routes(of: w)
    if !tracks.isEmpty {
        try gpx(tracks, hr: hr, title: w.title, type: w.workoutActivityType.kind.slug, iso: iso)
            .write(to: dir.appendingPathComponent("route.gpx"), atomically: true, encoding: .utf8)
        files.append("route.gpx")
    }

    files.insert("workout.json", at: 0)
    let json = try JSONSerialization.data(withJSONObject: summary(w, files: files, iso: iso), options: [.prettyPrinted, .sortedKeys])
    try json.write(to: dir.appendingPathComponent("workout.json"), options: .atomic)
    return dir
}

private func summary(_ w: HKWorkout, files: [String], iso: ISO8601DateFormatter) -> [String: Any] {
    let kind = w.workoutActivityType.kind
    var out: [String: Any] = [
        "format": "polarlab-health-workout/1",
        "type": kind.slug,
        "type_name": kind.name,
        "activity_type_raw": Int(w.workoutActivityType.rawValue),
        "indoor": w.isIndoor,
        "start": iso.string(from: w.startDate),
        "end": iso.string(from: w.endDate),
        "duration_s": w.duration,
        "uuid": w.uuid.uuidString,
        "files": files,
    ]
    let src = w.sourceRevision
    var source: [String: Any] = ["name": src.source.name, "bundle_id": src.source.bundleIdentifier]
    if let v = src.version { source["version"] = v }
    if let p = src.productType { source["product_type"] = p }
    out["source"] = source
    if let d = w.device {
        var device: [String: Any] = [:]
        for (k, v) in [("name", d.name), ("model", d.model), ("manufacturer", d.manufacturer),
                       ("hardware", d.hardwareVersion), ("software", d.softwareVersion)] {
            if let v { device[k] = v }
        }
        out["device"] = device
    }

    var stats: [String: Any] = [:]
    for m in HealthMetric.all {
        guard let s = w.statistics(for: m.type) else { continue }
        var e: [String: Any] = ["unit": m.unitName]
        for (k, q) in [("sum", s.sumQuantity()), ("avg", s.averageQuantity()), ("min", s.minimumQuantity()), ("max", s.maximumQuantity())] {
            if let v = q?.doubleValue(for: m.unit), v.isFinite { e[k] = v }
        }
        stats[m.key] = e
    }
    out["statistics"] = stats

    var meta: [String: Any] = [:]
    for (k, v) in w.metadata ?? [:] {
        switch v {
        case let q as HKQuantity: meta[k] = q.description
        case let d as Date: meta[k] = iso.string(from: d)
        case let n as NSNumber: meta[k] = n
        case let s as String: meta[k] = s
        default: meta[k] = String(describing: v)
        }
    }
    out["metadata"] = meta

    out["events"] = (w.workoutEvents ?? []).map { e -> [String: Any] in
        ["type": e.type.name, "start": iso.string(from: e.dateInterval.start), "end": iso.string(from: e.dateInterval.end)]
    }
    out["activities"] = w.workoutActivities.map { a -> [String: Any] in
        var e: [String: Any] = ["type": a.workoutConfiguration.activityType.kind.slug, "start": iso.string(from: a.startDate)]
        if let end = a.endDate { e["end"] = iso.string(from: end) }
        return e
    }
    return out
}

/// GPX 1.1 con la FC su ogni punto (estensione Garmin, letta da Strava, GoldenCheetah, gpx.studio…).
private func gpx(_ tracks: [[CLLocation]], hr: [SeriesPoint], title: String, type: String, iso: ISO8601DateFormatter) -> String {
    let hrTimes = hr.map(\.start)
    var s = """
    <?xml version="1.0" encoding="UTF-8"?>
    <gpx version="1.1" creator="PolarLab" xmlns="http://www.topografix.com/GPX/1/1" \
    xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">
    <metadata><time>\(iso.string(from: tracks[0][0].timestamp))</time></metadata>
    <trk><name>\(title)</name><type>\(type)</type>

    """
    for locs in tracks {
        s += "<trkseg>\n"
        for l in locs {
            s += "<trkpt lat=\"\(num(l.coordinate.latitude, 7))\" lon=\"\(num(l.coordinate.longitude, 7))\">"
            if l.verticalAccuracy >= 0 { s += "<ele>\(num(l.altitude, 1))</ele>" }
            s += "<time>\(iso.string(from: l.timestamp))</time>"
            if let bpm = nearest(hrTimes, l.timestamp).map({ hr[$0].value }) {
                s += "<extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>\(Int(bpm.rounded()))</gpxtpx:hr>"
                    + "</gpxtpx:TrackPointExtension></extensions>"
            }
            s += "</trkpt>\n"
        }
        s += "</trkseg>\n"
    }
    return s + "</trk>\n</gpx>\n"
}

/// Indice dell'ultima misura non successiva a `t`, se è entro 15 s.
private func nearest(_ times: [Date], _ t: Date) -> Int? {
    var lo = 0, hi = times.count
    while lo < hi {
        let mid = (lo + hi) / 2
        if times[mid] <= t { lo = mid + 1 } else { hi = mid }
    }
    guard lo > 0, t.timeIntervalSince(times[lo - 1]) <= 15 else { return nil }
    return lo - 1
}

private func num(_ v: Double, _ digits: Int) -> String {
    v.isFinite ? String(format: "%.\(digits)f", v) : ""
}

private func field(_ s: String) -> String {
    s.contains(where: { $0 == "," || $0 == "\"" || $0 == "\n" }) ? "\"" + s.replacingOccurrences(of: "\"", with: "\"\"") + "\"" : s
}

private extension HKWorkoutEventType {
    var name: String {
        switch self {
        case .pause: "pause"
        case .resume: "resume"
        case .lap: "lap"
        case .marker: "marker"
        case .motionPaused: "motion_paused"
        case .motionResumed: "motion_resumed"
        case .segment: "segment"
        case .pauseOrResumeRequest: "pause_or_resume_request"
        @unknown default: "other_\(rawValue)"
        }
    }
}
