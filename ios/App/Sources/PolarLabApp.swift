import SwiftUI

@main
struct PolarLabApp: App {
    @State private var model = LiveModel()
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            TabView {
                LiveView()
                    .tabItem { Label("Corsa", systemImage: "heart.text.square") }
                SessionsView()
                    .tabItem { Label("Sessioni", systemImage: "list.bullet.rectangle") }
                HealthView()
                    .tabItem { Label("Salute", systemImage: "heart.circle") }
            }
            .environment(model)
            .preferredColorScheme(.dark)
            .tint(Theme.accent)
        }
        .onChange(of: phase) { _, p in
            switch p {
            case .active: model.becameActive()
            case .background: model.wentBackground()
            default: break
            }
        }
    }
}
