// Schermata di corsa: pochi numeri grandi, leggibili con un'occhiata.

import PolarKit
import SwiftUI

enum Theme {
    static let accent = Color(red: 1.0, green: 0.27, blue: 0.33)
    static let ecg = Color(red: 0.35, green: 1.0, blue: 0.55)
    static let card = Color.white.opacity(0.07)

    static func zone(_ z: Alpha1Zone) -> Color {
        switch z {
        case .aerobic: .green
        case .between: .orange
        case .aboveVT2: .red
        case .none: .gray
        }
    }

    static func quality(_ q: Double) -> Color {
        !q.isFinite ? .gray : q >= 85 ? .green : q >= 65 ? .yellow : q >= 35 ? .orange : .red
    }
}

func fmt(_ v: Double, _ digits: Int = 0) -> String {
    v.isFinite ? String(format: "%.\(digits)f", v) : "—"
}

func fmtDuration(_ s: Double) -> String {
    let t = Int(s.rounded())
    return t >= 3600 ? String(format: "%d:%02d:%02d", t / 3600, t / 60 % 60, t % 60) : String(format: "%d:%02d", t / 60, t % 60)
}

struct LiveView: View {
    @Environment(LiveModel.self) private var model
    @State private var showSettings = false

    var body: some View {
        NavigationStack {
            VStack(spacing: 14) {
                StatusBar()
                HeartRate()
                ZoneCard()
                MetricGrid()
                if model.showECG && model.isStreaming {
                    EcgStrip(snapshot: model.ecg)
                        .frame(height: 110)
                        .background(Theme.card, in: RoundedRectangle(cornerRadius: 14))
                }
                Spacer(minLength: 0)
                ActionButtons()
            }
            .padding(.horizontal, 16)
            .padding(.bottom, 8)
            .navigationTitle("PolarLab")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button { showSettings = true } label: { Image(systemName: "gearshape") }
                }
            }
            .sheet(isPresented: $showSettings) { SettingsView() }
            .alert("Errore", isPresented: .constant(model.lastError != nil)) {
                Button("OK") { model.lastError = nil }
            } message: { Text(model.lastError ?? "") }
        }
    }
}

private struct StatusBar: View {
    @Environment(LiveModel.self) private var model

    var body: some View {
        HStack(spacing: 8) {
            Circle().fill(color).frame(width: 9, height: 9)
            Text(model.isDemo ? "Demo · registrazione di prova" : model.state.label)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            Spacer()
            if let b = model.metrics.battery {
                Label("\(b)%", systemImage: b > 20 ? "battery.75" : "battery.25")
                    .font(.caption)
                    .foregroundStyle(b > 20 ? Color.secondary : Color.red)
            }
            if model.metrics.contact == false {
                Label("Contatto", systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
        }
        .padding(.top, 4)
    }

    private var color: Color {
        switch model.state {
        case .streaming: .green
        case .scanning, .connecting, .configuring, .reconnecting: .orange
        case .unavailable: .red
        case .idle: .gray
        }
    }
}

private struct HeartRate: View {
    @Environment(LiveModel.self) private var model

    var body: some View {
        let m = model.metrics
        let hr = m.hrEcg.isFinite ? m.hrEcg : m.hrStrap
        VStack(spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(fmt(hr))
                    .font(.system(size: 96, weight: .bold, design: .rounded))
                    .monospacedDigit()
                    .contentTransition(.numericText())
                Text("bpm").font(.title3).foregroundStyle(.secondary)
            }
            Text(m.hrStrap.isFinite ? "fascia \(fmt(m.hrStrap)) · battiti \(m.beats)" : " ")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
    }
}

private struct ZoneCard: View {
    @Environment(LiveModel.self) private var model

    var body: some View {
        let a1 = model.metrics.dfaA1
        let zone = Alpha1Zone(a1)
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text("DFA α1").font(.caption).foregroundStyle(.secondary)
                Text(fmt(a1, 2)).font(.title.bold()).monospacedDigit()
            }
            Spacer()
            Text(zone == .none ? "servono ~2 min di battiti" : zone.rawValue)
                .font(zone == .none ? .footnote : .title3.weight(.semibold))
                .foregroundStyle(zone == .none ? Color.secondary : Theme.zone(zone))
        }
        .padding(14)
        .background(Theme.zone(zone).opacity(zone == .none ? 0.08 : 0.18), in: RoundedRectangle(cornerRadius: 14))
    }
}

private struct MetricGrid: View {
    @Environment(LiveModel.self) private var model

    var body: some View {
        let m = model.metrics
        Grid(horizontalSpacing: 10, verticalSpacing: 10) {
            GridRow {
                Tile(title: "Cadenza", value: fmt(m.cadenceSpm), unit: "passi/min")
                Tile(title: "RMSSD", value: fmt(m.rmssd, 1), unit: "ms")
            }
            GridRow {
                Tile(title: "Respiro", value: fmt(m.respBrpm), unit: "atti/min")
                Tile(title: "Qualità", value: fmt(m.quality), unit: m.qualityLabel, color: Theme.quality(m.quality))
            }
        }
    }
}

private struct Tile: View {
    let title: String
    let value: String
    let unit: String
    var color: Color = .primary

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                Text(value).font(.title2.bold()).monospacedDigit().foregroundStyle(color)
                Text(unit).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 14))
    }
}

private struct ActionButtons: View {
    @Environment(LiveModel.self) private var model

    var body: some View {
        VStack(spacing: 10) {
            if model.isStreaming {
                Button(action: model.toggleRecording) {
                    HStack {
                        Image(systemName: model.isRecording ? "stop.fill" : "record.circle")
                        if let since = model.recordingSince {
                            // Si aggiorna una volta al secondo, e solo con lo schermo acceso
                            TimelineView(.periodic(from: since, by: 1)) { ctx in
                                Text("Termina · \(fmtDuration(ctx.date.timeIntervalSince(since)))").monospacedDigit()
                            }
                        } else {
                            Text("Inizia registrazione")
                        }
                    }
                    .font(.title3.weight(.semibold))
                    .frame(maxWidth: .infinity, minHeight: 56)
                }
                .buttonStyle(.borderedProminent)
                .tint(model.isRecording ? .red : Theme.accent)
            } else if case .idle = model.state {
                Button("Collega la fascia", action: model.connect)
                    .font(.title3.weight(.semibold))
                    .frame(maxWidth: .infinity, minHeight: 56)
                    .buttonStyle(.borderedProminent)
                Button("Prova senza fascia (demo)", action: model.startDemo)
                    .font(.footnote)
            } else if case .unavailable = model.state {
                Button("Prova senza fascia (demo)", action: model.startDemo)
                    .font(.footnote)
            } else {
                Text("Indossa la fascia con gli elettrodi umidi").font(.footnote).foregroundStyle(.secondary)
                Button("Annulla", action: model.disconnect).font(.footnote)
            }
        }
    }
}
