// Scheda «Salute»: allenamenti di Apple Health e condivisione di ognuno come .zip per il Mac.

import CoreTransferable
import HealthKit
import SwiftUI

struct HealthView: View {
    @State private var health = HealthStore()

    var body: some View {
        NavigationStack {
            List {
                if !health.available {
                    ContentUnavailableView("Salute non disponibile", systemImage: "heart.slash",
                                           description: Text("Questo dispositivo non ha l'app Salute."))
                } else if let e = health.error {
                    ContentUnavailableView("Impossibile leggere Salute", systemImage: "exclamationmark.triangle",
                                           description: Text(e))
                } else if health.workouts.isEmpty && !health.loading {
                    ContentUnavailableView("Nessun allenamento", systemImage: "figure.run",
                                           description: Text("Negli ultimi \(health.days) giorni non ci sono allenamenti, "
                                                             + "oppure la lettura è negata: Impostazioni → Salute → "
                                                             + "Accesso ai dati e dispositivi → PolarLab."))
                }
                ForEach(health.workouts, id: \.uuid) { w in
                    NavigationLink(value: w) { WorkoutRow(w: w) }
                }
                if health.available && health.error == nil {
                    Section {
                        Button {
                            Task { await health.load(more: true) }
                        } label: {
                            if health.loading {
                                ProgressView()
                            } else {
                                Label("Mostra \(HealthStore.pageDays) giorni in più", systemImage: "clock.arrow.circlepath")
                            }
                        }
                        .disabled(health.loading)
                    } footer: {
                        Text("Ultimi \(health.days) giorni. Ogni allenamento si condivide come .zip con riepilogo, "
                             + "frequenza cardiaca, percorso GPS e le altre serie registrate.")
                    }
                }
            }
            .navigationTitle("Salute")
            .navigationDestination(for: HKWorkout.self) { WorkoutDetail(w: $0) }
            .refreshable { await health.load() }
            .task { if health.workouts.isEmpty { await health.load() } }
        }
    }
}

private struct WorkoutRow: View {
    let w: HKWorkout

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label(w.title, systemImage: w.workoutActivityType.kind.symbol).font(.headline)
            HStack(spacing: 12) {
                Text(w.startDate.formatted(.dateTime.weekday(.abbreviated).day().month(.abbreviated).hour().minute()))
                Label(fmtDuration(w.duration), systemImage: "timer")
                if let d = w.distanceM, d > 0 { Text(fmtKm(d)) }
                if let hr = w.avgHr { Label(fmt(hr), systemImage: "heart.fill") }
            }
            .font(.subheadline)
            .foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
    }
}

private struct WorkoutDetail: View {
    let w: HKWorkout

    var body: some View {
        List {
            Section("Riepilogo") {
                Row("Inizio", w.startDate.formatted(date: .abbreviated, time: .shortened))
                Row("Durata", fmtDuration(w.duration))
                Row("Distanza", w.distanceM.map(fmtKm) ?? "—")
                Row("Energia attiva", w.activeKcal.map { "\(fmt($0)) kcal" } ?? "—")
                Row("FC media", w.avgHr.map { "\(fmt($0)) bpm" } ?? "—")
                Row("FC massima", w.maxHr.map { "\(fmt($0)) bpm" } ?? "—")
                Row("Registrato da", w.sourceRevision.source.name)
            }
            Section {
                ShareLink(item: WorkoutArchive(workout: w),
                          preview: SharePreview(w.title, image: Image(systemName: w.workoutActivityType.kind.symbol))) {
                    Label("Condividi (.zip con JSON, CSV e GPX)", systemImage: "square.and.arrow.up")
                }
            } footer: {
                Text("workout.json (riepilogo, eventi, metadati), heart_rate.csv, series.csv (passo, cadenza, "
                     + "potenza…) e route.gpx se c'è il GPS. I file assenti in Salute non vengono creati.")
            }
        }
        .navigationTitle(w.title)
    }
}

private func fmtKm(_ m: Double) -> String { "\(fmt(m / 1000, 2)) km" }

/// Un allenamento come .zip, creato solo quando si condivide.
struct WorkoutArchive: Transferable {
    let workout: HKWorkout

    static var transferRepresentation: some TransferRepresentation {
        FileRepresentation(exportedContentType: .zip) { archive in
            let dir = try await exportWorkout(archive.workout)
            return SentTransferredFile(try zipFolder(dir, named: dir.lastPathComponent + ".zip"))
        }
    }
}
