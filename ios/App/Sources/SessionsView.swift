// Elenco delle corse registrate, dettaglio con grafici e condivisione.

import Charts
import PolarKit
import SwiftUI

struct SessionsView: View {
    @Environment(LiveModel.self) private var model

    var body: some View {
        let store = model.store
        NavigationStack {
            List {
                if store.sessions.isEmpty {
                    ContentUnavailableView("Nessuna corsa registrata", systemImage: "figure.run",
                                           description: Text("Collega la fascia e premi «Inizia registrazione»."))
                }
                ForEach(store.sessions) { s in
                    NavigationLink(value: s) { SessionRow(s: s, processing: store.processing.contains(s.id),
                                                          recording: model.recordingDir == s.url) }
                }
                .onDelete { idx in
                    for i in idx where store.sessions[i].url != model.recordingDir { store.delete(store.sessions[i]) }
                }
                if !store.sessions.isEmpty {
                    Section {
                        Text("I file sono anche nell'app File → Sul mio iPhone → PolarLab → Sessioni.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .navigationTitle("Sessioni")
            .navigationDestination(for: SessionInfo.self) { SessionDetail(s: $0) }
            .refreshable { store.reload() }
        }
    }
}

private struct SessionRow: View {
    let s: SessionInfo
    let processing: Bool
    let recording: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(s.title.capitalized).font(.headline)
            HStack(spacing: 12) {
                if recording {
                    Label("in registrazione", systemImage: "record.circle").foregroundStyle(.red)
                } else if processing {
                    ProgressView().controlSize(.small)
                    Text("elaborazione…")
                } else {
                    if let d = s.durationS { Label(fmtDuration(d), systemImage: "timer") }
                    if let hr = s.avgHr { Label(fmt(hr), systemImage: "heart.fill") }
                    if let c = s.avgCadence { Label(fmt(c), systemImage: "figure.run") }
                }
            }
            .font(.subheadline)
            .foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
    }
}

struct SessionDetail: View {
    @Environment(LiveModel.self) private var model
    let s: SessionInfo
    @State private var series: [MetricPoint] = []

    var body: some View {
        let current = model.store.sessions.first { $0.id == s.id } ?? s
        List {
            Section("Riepilogo") {
                Row("Durata", current.durationS.map(fmtDuration) ?? "—")
                Row("FC media", current.avgHr.map { "\(fmt($0)) bpm" } ?? "—")
                Row("FC massima", current.maxHr.map { "\(fmt($0)) bpm" } ?? "—")
                Row("Cadenza media", current.avgCadence.map { "\(fmt($0)) passi/min" } ?? "—")
                Row("Sopra VT1 (α1 < 0.75)", current.minAboveVT1.map { "\(fmt($0)) min" } ?? "—")
                Row("Sopra VT2 (α1 < 0.50)", current.minAboveVT2.map { "\(fmt($0)) min" } ?? "—")
                Row("Battiti corretti", current.artifactPct.map { "\(fmt($0, 1)) %" } ?? "—")
                Row("Buchi nello stream", current.gaps.map(String.init) ?? "—")
            }
            if !series.isEmpty {
                Section("Frequenza cardiaca") {
                    Chart(series) { p in
                        if p.hr.isFinite { LineMark(x: .value("min", p.t / 60), y: .value("bpm", p.hr)) }
                    }
                    .foregroundStyle(Theme.accent)
                    .chartYScale(domain: .automatic(includesZero: false))
                    .frame(height: 160)
                }
                Section("DFA α1") {
                    Chart {
                        RuleMark(y: .value("VT1", aetAlpha1)).foregroundStyle(.green.opacity(0.5))
                        RuleMark(y: .value("VT2", antAlpha1)).foregroundStyle(.red.opacity(0.5))
                        ForEach(series.filter { $0.a1.isFinite }) { p in
                            PointMark(x: .value("min", p.t / 60), y: .value("α1", p.a1)).symbolSize(8)
                        }
                    }
                    .frame(height: 140)
                }
            }
            Section {
                ShareLink(item: SessionArchive(dir: current.url),
                          preview: SharePreview("PolarLab \(current.id)", image: Image(systemName: "waveform.path.ecg"))) {
                    Label("Condividi (.zip con raw.plab e CSV)", systemImage: "square.and.arrow.up")
                }
                .disabled(model.store.processing.contains(current.id) || model.recordingDir == current.url)
            } footer: {
                Text("Sul Mac: python -m polarlab --replay <cartella> per rivederla in PolarLab. "
                     + "Dimensione: \(ByteCountFormatter.string(fromByteCount: current.sizeBytes, countStyle: .file)).")
            }
        }
        .navigationTitle(current.date?.formatted(date: .abbreviated, time: .shortened) ?? current.id)
        .task(id: current.finished) { series = await MetricPoint.load(current.url) }
    }
}

struct Row: View {
    let title: String
    let value: String
    init(_ title: String, _ value: String) {
        self.title = title
        self.value = value
    }

    var body: some View {
        HStack {
            Text(title)
            Spacer()
            Text(value).foregroundStyle(.secondary).monospacedDigit()
        }
    }
}

struct MetricPoint: Identifiable {
    let id: Int
    let t: Double
    let hr: Double
    let a1: Double

    /// Legge metrics.csv (1 riga al secondo) e lo riduce a ~600 punti per il grafico.
    static func load(_ dir: URL) async -> [MetricPoint] {
        await Task.detached(priority: .utility) {
            guard let text = try? String(contentsOf: dir.appendingPathComponent("metrics.csv"), encoding: .utf8) else { return [] }
            let lines = text.split(separator: "\n")
            guard let header = lines.first?.split(separator: ",", omittingEmptySubsequences: false).map(String.init),
                  let it = header.firstIndex(of: "t"), let ih = header.firstIndex(of: "hr_ecg"),
                  let ia = header.firstIndex(of: "dfa_a1") else { return [] }
            let step = max(1, (lines.count - 1) / 600)
            return stride(from: 1, to: lines.count, by: step).enumerated().compactMap { k, i in
                let c = lines[i].split(separator: ",", omittingEmptySubsequences: false)
                guard c.count > max(it, ih, ia) else { return nil }
                return MetricPoint(id: k, t: Double(c[it]) ?? .nan, hr: Double(c[ih]) ?? .nan, a1: Double(c[ia]) ?? .nan)
            }
        }.value
    }
}
