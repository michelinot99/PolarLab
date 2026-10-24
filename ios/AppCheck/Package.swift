// swift-tools-version:6.0
// Controllo dei tipi dei sorgenti dell'app iPhone senza Xcode: si lancia ./check.sh,
// che copia App/Sources in Sources/ (cartella generata).
import PackageDescription

let package = Package(
    name: "AppCheck",
    platforms: [.macOS(.v14)],
    dependencies: [.package(path: "../PolarKit")],
    targets: [.executableTarget(name: "AppCheck", dependencies: [.product(name: "PolarKit", package: "PolarKit")], path: "Sources")],
    swiftLanguageModes: [.v5]
)
