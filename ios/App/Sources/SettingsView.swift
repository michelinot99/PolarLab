import PolarKit
import SwiftUI

struct SettingsView: View {
    @Environment(LiveModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        @Bindable var model = model
        NavigationStack {
            Form {
                Section {
                    Toggle("Registra appena la fascia è collegata", isOn: $model.autoRecord)
                } header: {
                    Text("Registrazione")
                } footer: {
                    Text("Ogni collegamento diventa una sessione: non si perde nessuna corsa. "
                         + "Dopo 10 minuti senza dati dalla fascia la registrazione si chiude da sola.")
                }
                Section {
                    Toggle("Mostra il tracciato ECG", isOn: $model.showECG)
                    Toggle("Schermo sempre acceso durante la registrazione", isOn: $model.keepScreenOn)
                } header: {
                    Text("Batteria")
                } footer: {
                    Text("Lo schermo è ciò che consuma di più: in corsa conviene lasciarlo spegnere, "
                         + "la registrazione continua comunque. Il tracciato ECG si disegna solo quando lo guardi.")
                }
                Section {
                    Toggle("Accelerometro (cadenza, oscillazione)", isOn: $model.accEnabled)
                    Toggle("Filtro rete 50 Hz sul tracciato", isOn: $model.notchEnabled)
                } header: {
                    Text("Fascia")
                } footer: {
                    Text("Senza accelerometro la fascia trasmette circa metà dei dati. Vale dal prossimo collegamento.")
                }
                Section("Polar H10") {
                    LabeledContent("Stato", value: model.isDemo ? "demo" : model.state.label)
                    if !model.strapName.isEmpty { LabeledContent("Nome", value: model.strapName) }
                    ForEach(["firmware", "serial", "mtu"], id: \.self) { k in
                        if let v = model.strapInfo[k] { LabeledContent(k.capitalized, value: v) }
                    }
                    ForEach(model.streams.sorted(by: { $0.key < $1.key }), id: \.key) { k, v in
                        LabeledContent(k, value: v)
                    }
                    if model.isStreaming || model.isDemo {
                        Button("Scollega") { model.disconnect() }
                    }
                    if H10Link.savedId != nil {
                        Button("Dimentica questa fascia", role: .destructive) { model.forgetStrap() }
                    }
                }
            }
            .navigationTitle("Impostazioni")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Fine") { dismiss() } }
            }
        }
    }
}
