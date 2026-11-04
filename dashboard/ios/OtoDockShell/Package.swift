// swift-tools-version: 5.9
import PackageDescription

// The shell's rules with no UIKit or WebKit in them, so `swift test` runs
// them on the Mac without a simulator: the origin and address rules, the
// installations registry, the sign-in flow, the saved-file rules. The app
// target links the library; the tests are the port of the Android unit tests.
let package = Package(
    name: "OtoDockShell",
    platforms: [.iOS(.v16), .macOS(.v13)],
    products: [
        .library(name: "OtoDockShell", targets: ["OtoDockShell"]),
    ],
    targets: [
        .target(name: "OtoDockShell"),
        .testTarget(name: "OtoDockShellTests", dependencies: ["OtoDockShell"]),
    ]
)
