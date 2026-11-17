// swift-tools-version:5.10
import PackageDescription

let package = Package(
    name: "Todone",
    platforms: [.macOS(.v14)],
    targets: [
        // Models, the helper transport and formatting: everything testable without a UI.
        .target(name: "TodoneCore"),
        .executableTarget(name: "Todone", dependencies: ["TodoneCore"]),
        .testTarget(name: "TodoneCoreTests", dependencies: ["TodoneCore"]),
    ]
)
