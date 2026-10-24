// swift-tools-version:6.0
// PolarKit: protocollo H10, DSP e motore di analisi, identici a polarlab/ (Python).
// Si compila anche solo con i Command Line Tools:
//     swift build --scratch-path build.nosync
//     swift run --scratch-path build.nosync plab selftest
import PackageDescription

let package = Package(
    name: "PolarKit",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [.library(name: "PolarKit", targets: ["PolarKit"])],
    targets: [
        .target(name: "PolarKit"),
        // Strumento da riga di comando per il Mac: autotest e confronto con le CSV di Python
        .executableTarget(name: "plab", dependencies: ["PolarKit"]),
    ],
    swiftLanguageModes: [.v5]
)
