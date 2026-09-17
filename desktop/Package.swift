// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "XHSMobileDesktop",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "XHSMobileDesktop", targets: ["XHSMobileDesktop"])],
    targets: [
        .executableTarget(name: "XHSMobileDesktop"),
    ],
    swiftLanguageModes: [.v5]
)
