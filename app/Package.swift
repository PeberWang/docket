// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "Desk",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "Desk", path: "Sources/Desk")
    ]
)
