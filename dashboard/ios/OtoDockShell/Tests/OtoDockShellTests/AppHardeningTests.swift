import XCTest
import Foundation

/// The app's shape, read from the tree (the twin of the Android app's
/// `AppManifestHardeningTest`): the Info.plist keys a store build needs and
/// the ones it must not carry, the entitlements, the privacy manifest, the
/// shim, the sounds, the project file's phases and settings, the Capacitor
/// config's iOS block, and a few rules the Swift sources must keep.
final class AppHardeningTests: XCTestCase {

    /// `dashboard/ios/` from this file's place in the tree.
    static let ios = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    static let app = ios.appendingPathComponent("App/App")

    private func plist(_ name: String) throws -> [String: Any] {
        let data = try Data(contentsOf: Self.app.appendingPathComponent(name))
        return try XCTUnwrap(PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any])
    }

    private func text(_ path: String) throws -> String {
        return try String(contentsOf: Self.ios.appendingPathComponent(path), encoding: .utf8)
    }

    func testTheInfoPlistCarriesTheUsageStringsTheStoresRead() throws {
        let info = try plist("Info.plist")
        for key in ["NSMicrophoneUsageDescription", "NSSpeechRecognitionUsageDescription", "NSCameraUsageDescription",
                    "NSLocationWhenInUseUsageDescription", "NSLocationAlwaysAndWhenInUseUsageDescription",
                    "NSLocalNetworkUsageDescription"] {
            let value = info[key] as? String ?? ""
            XCTAssertFalse(value.isEmpty, key)
            XCTAssertTrue(value.hasPrefix("OtoDock") || value.hasPrefix("When one of your agents"), "\(key) names what the app does")
        }
        // The app never asks for background location, but the location plugin
        // references the API, so App Store Connect asks for the string (ITMS-90683).
        XCTAssertNil(info["NSLocationAlwaysUsageDescription"])
        XCTAssertNil(info["NSPhotoLibraryUsageDescription"], "attaching goes through the system picker")
        XCTAssertNil(info["NSPhotoLibraryAddUsageDescription"], "Save writes to Files")
        XCTAssertNil(info["NSFaceIDUsageDescription"], "passkeys go through the system")
    }

    func testTheInfoPlistAnswersExportComplianceAndHoldsFirebaseBack() throws {
        let info = try plist("Info.plist")
        // The sign-in session's sheet names the app by CFBundleName: the
        // product name ("App") would read "App" wants to use <host> to sign in.
        XCTAssertEqual("OtoDock", info["CFBundleName"] as? String)
        XCTAssertEqual("OtoDock", info["CFBundleDisplayName"] as? String)
        XCTAssertEqual(false, info["ITSAppUsesNonExemptEncryption"] as? Bool)
        XCTAssertEqual(false, info["FirebaseAppDelegateProxyEnabled"] as? Bool, "the app hands Firebase the token itself")
        XCTAssertEqual(false, info["FirebaseMessagingAutoInitEnabled"] as? Bool, "no token before notifications are allowed")
        XCTAssertNil(info["UIBackgroundModes"], "no background mode: the alarm is an alert, not a service")
    }

    /// The iPad window opens at the screen's size and stays resizable: the
    /// scene raises its minimum size while it opens, never the plist key that
    /// drops the resizing; and in a window the page starts below the window
    /// controls, through the corner-adapted safe area.
    func testTheIPadWindowOpensFullScreenAndStartsBelowTheWindowControls() throws {
        let info = try plist("Info.plist")
        XCTAssertNil(info["UIRequiresFullScreen"], "the window stays resizable")
        let scene = try text("App/App/SceneDelegate.swift")
        XCTAssertTrue(scene.contains("restrictions.minimumSize = Self.nearlyTheScreen(of: windowScene, short: 1)"), "opens at the screen's size")
        XCTAssertTrue(scene.contains("restrictions.minimumSize = Self.nearlyTheScreen(of: windowScene, short: 2)"), "a second, different minimum once active and still small")
        XCTAssertTrue(scene.contains("restrictions.minimumSize = size"), "put back once the window is there")
        XCTAssertTrue(scene.contains("DispatchQueue.main.asyncAfter(deadline: .now() + 1.5)"), "and after a moment either way")
        let shell = try text("App/App/ShellViewController.swift")
        XCTAssertTrue(shell.contains("if #available(iOS 26, *) {\n            return view.edgeInsets(for: .safeArea(cornerAdaptation: .vertical))"), "the corner-adapted region on iPadOS 26")
        XCTAssertFalse(shell.contains("let safe = container.safeAreaInsets"), "the plain safe area leaves the window controls out")
    }

    func testTheInfoPlistRegistersNoUrlSchemeAndNoStoryboardButTheLaunchScreen() throws {
        let info = try plist("Info.plist")
        XCTAssertNil(info["CFBundleURLTypes"], "the sign-in session needs no scheme and any app could claim one")
        XCTAssertNil(info["UIMainStoryboardFile"])
        XCTAssertEqual("LaunchScreen", info["UILaunchStoryboardName"] as? String)
        let scenes = try XCTUnwrap(info["UIApplicationSceneManifest"] as? [String: Any])
        let roles = try XCTUnwrap(scenes["UISceneConfigurations"] as? [String: Any])
        let application = try XCTUnwrap(roles["UIWindowSceneSessionRoleApplication"] as? [[String: Any]])
        XCTAssertEqual("$(PRODUCT_MODULE_NAME).SceneDelegate", application.first?["UISceneDelegateClassName"] as? String)
        XCTAssertNil(application.first?["UISceneStoryboardFile"], "the scene delegate builds the window")
        XCTAssertEqual(true, info["UIFileSharingEnabled"] as? Bool, "the saved files show in the Files app")
        XCTAssertEqual(true, info["LSSupportsOpeningDocumentsInPlace"] as? Bool)
    }

    func testTransportSecurityAllowsTheLocalNetworkAndNothingMore() throws {
        let info = try plist("Info.plist")
        let ats = try XCTUnwrap(info["NSAppTransportSecurity"] as? [String: Any])
        XCTAssertEqual(true, ats["NSAllowsLocalNetworking"] as? Bool)
        XCTAssertNil(ats["NSAllowsArbitraryLoads"])
        XCTAssertNil(ats["NSAllowsArbitraryLoadsInWebContent"])
        XCTAssertNil(ats["NSExceptionDomains"])
    }

    func testTheEntitlementsCarryPushAndTimeSensitiveNotifications() throws {
        let entitlements = try plist("App.entitlements")
        XCTAssertEqual("development", entitlements["aps-environment"] as? String, "the store export switches it to production")
        XCTAssertEqual(true, entitlements["com.apple.developer.usernotifications.time-sensitive"] as? Bool)
        XCTAssertNil(entitlements["com.apple.developer.usernotifications.critical-alerts"], "the operator's decision after the first release")
    }

    func testThePrivacyManifestNamesTheReasonsForTheApisTheShellUses() throws {
        let manifest = try plist("PrivacyInfo.xcprivacy")
        XCTAssertEqual(false, manifest["NSPrivacyTracking"] as? Bool)
        XCTAssertEqual(0, (manifest["NSPrivacyTrackingDomains"] as? [Any])?.count)
        XCTAssertEqual(0, (manifest["NSPrivacyCollectedDataTypes"] as? [Any])?.count, "what Firebase collects is in its own manifest")
        let apis = try XCTUnwrap(manifest["NSPrivacyAccessedAPITypes"] as? [[String: Any]])
        let reasons = Dictionary(uniqueKeysWithValues: apis.map { ($0["NSPrivacyAccessedAPIType"] as? String ?? "", $0["NSPrivacyAccessedAPITypeReasons"] as? [String] ?? []) })
        XCTAssertEqual(["CA92.1"], reasons["NSPrivacyAccessedAPICategoryUserDefaults"])
        XCTAssertNil(reasons["NSPrivacyAccessedAPICategoryFileTimestamp"], "the shell reads no file timestamp (fileExists is not one)")
        XCTAssertNil(reasons["NSPrivacyAccessedAPICategorySystemBootTime"], "the flow's clock is not a boot-time API")
    }

    func testTheSwiftSourcesKeepTheRulesTheManifestRestsOn() throws {
        let sources = try FileManager.default.contentsOfDirectory(at: Self.app, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "swift" }
        XCTAssertGreaterThanOrEqual(sources.count, 10)
        var all = ""
        for file in sources { all += try String(contentsOf: file, encoding: .utf8) }
        XCTAssertFalse(all.contains("systemUptime"), "a boot-time API needs a reason in the privacy manifest")
        XCTAssertFalse(all.contains("mach_absolute_time"))
        XCTAssertTrue(all.contains("clock_gettime_nsec_np(CLOCK_MONOTONIC)") || all.contains("MonotonicClock.nowMs()"), "the flow's clock")
        XCTAssertFalse(all.contains("appStartPath = \"/"), "a route never rides the start path: the bridge exits on a missing file")
        // The judge decides every message, plugin call and prompt (ChannelJudge, tested on its own).
        XCTAssertTrue(all.contains("ChannelJudge.judgeChannel("), "the channel's messages go to the judge")
        XCTAssertTrue(all.contains("ChannelJudge.judgeBridge("), "Capacitor's handler goes to the judge")
        XCTAssertTrue(all.contains("ChannelJudge.promptAnswer(prompt)"), "the prompt channel is answered, never forwarded")
        XCTAssertFalse(all.contains("NativeBridgePolicy.sameOrigin(active, origin)"), "no origin check outside the judge on a message")
        let judge = try text("OtoDockShell/Sources/OtoDockShell/ChannelJudge.swift")
        for plugin in ["CapacitorHttp", "CapacitorCookies", "WebView"] {
            XCTAssertTrue(judge.contains("\"\(plugin)\""), "the \(plugin) plugin is refused by name")
        }
        XCTAssertTrue(judge.contains("text.utf16.count <= NativeBridgePolicy.maxMessageChars"), "the cap counts UTF-16 units, Android's length()")
        XCTAssertTrue(all.contains("removeScriptMessageHandler(forName: \"bridge\")"), "Capacitor's handler is replaced under the same name")
        XCTAssertTrue(all.contains("UNUserNotificationCenter.current().delegate = self"), "the app's own notification delegate")
        XCTAssertFalse(all.contains("FirebaseApp.configure()\n        UNUserNotificationCenter"), "configure only with the config in the bundle")
        XCTAssertTrue(all.contains("unlessOpen: AuthBrowser.shared.isOpen"), "a second sign-in request is refused before the flow is armed")
        XCTAssertFalse(all.contains("AuthBrowser(shell:"), "one sign-in browser for the process, not one per shell")
        let auth = try text("App/App/AuthBrowser.swift")
        XCTAssertTrue(auth.contains("ShellViewController.current else { return }"), "the result goes to the shell that is current")
        XCTAssertFalse(auth.contains("weak var shell"))
        XCTAssertFalse(all.contains("attributesOfItem") || all.contains("modificationDate") || all.contains("creationDate"),
                       "no file timestamp API: the privacy manifest declares none")
        XCTAssertTrue(all.contains("from: window.screen.coordinateSpace)"), "the keyboard frame is in the screen's space")
        XCTAssertFalse(all.contains("from: window.coordinateSpace)"))
        XCTAssertFalse(all.contains("as? CapacitorBridge else { return }"), "the shell fails closed without its judge")
        XCTAssertTrue(all.contains("saveBase64Fits(base64.utf16.count)"))
        XCTAssertFalse(all.contains("saveBase64Fits(base64.count)"))
        // Firebase 13 deprecates every token fetch: the token comes through the
        // delegate alone, cached for each registration the dashboard asks for.
        XCTAssertFalse(all.contains("messaging.token {") || all.contains("messaging.fcmToken") || all.contains("messaging().fcmToken"),
                       "no deprecated token fetch")
        XCTAssertTrue(all.contains("didReceiveRegistrationToken fcmToken: String?) {"), "the delegate is the token's source")
        XCTAssertTrue(all.contains("if let fcmToken { postRegistration(fcmToken) }"), "a registration is answered from the kept token")
        XCTAssertTrue(all.contains("#if DEBUG\n        messaging.setAPNSToken(deviceToken, type: .sandbox)"),
                      "a Debug build registers a sandbox token: Firebase takes a simulator for a production device")
    }

    /// Every sentence a person reads in the shell (a toast, a dialog, the
    /// setup page's answers, the usage strings) keeps the reader rules the
    /// dashboard's `plainSentences.test.ts` holds: no semicolon, no dash, no
    /// arrow, nothing in brackets, no word for how the platform works inside,
    /// a capital first, at most 20 words a sentence. A string literal counts
    /// when it reads as prose (three words or more, a capital first or a
    /// full stop last) and is not a log line or a code path.
    func testTheShellsSentencesArePlain() throws {
        var sentences: [(place: String, text: String)] = []
        let info = try plist("Info.plist")
        for (key, value) in info where key.hasSuffix("UsageDescription") {
            sentences.append(("Info.plist \(key)", value as? String ?? ""))
        }
        let sources = try FileManager.default.contentsOfDirectory(at: Self.app, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "swift" }.sorted { $0.lastPathComponent < $1.lastPathComponent }
        for file in sources {
            let lines = try String(contentsOf: file, encoding: .utf8).components(separatedBy: "\n")
            for (index, line) in lines.enumerated() {
                if Self.NOT_FOR_PEOPLE.contains(where: { line.contains($0) }) { continue }
                for literal in Self.stringLiterals(in: line) where Self.readsAsProse(literal) {
                    sentences.append(("\(file.lastPathComponent):\(index + 1)", literal))
                }
            }
        }
        XCTAssertGreaterThanOrEqual(sentences.count, 15, "the shell's sentences were found")
        XCTAssertTrue(sentences.contains { $0.text.hasPrefix("The server did not answer.") }, "the setup page's answers are read")
        let faults = sentences.flatMap { s in Self.plainFaults(s.text).map { "\(s.place): \"\(s.text)\": \($0)" } }
        XCTAssertEqual(faults, [], faults.joined(separator: "\n"))
    }

    /// A line whose literals answer the log, the compiler or the file system, never a person.
    private static let NOT_FOR_PEOPLE = ["log.", "Logger(", "fatalError(", "precondition", "assert", "forResource:",
                                         "appendingPathComponent", "UserDefaults", "forKey:", "NSError(", "import ",
                                         "evaluateJavaScript", "jsQuote", "#filePath", "URL(string:"]

    /// The changelog lint's words for how the platform works inside, the dashboard test's list.
    private static let MECHANISM_WORDS = [
        "carve", "floor", "hook", "broker", "probe", "egress", "daemon", "endpoint", "handler", "payload", "regex",
        "mutex", "socket", "stdout", "stderr", "cgroup", "namespace", "userns", "asyncio", "coroutine", "mixin",
        "adapter", "tombstone", "manifest", "runtime", "sentinel", "heuristic", "idempotent", "pool",
    ]

    /// The double-quoted literals of one line, an interpolation read as one word.
    static func stringLiterals(in line: String) -> [String] {
        var out: [String] = []
        var current: String? = nil
        var depth = 0
        var i = line.startIndex
        while i < line.endIndex {
            let c = line[i]
            if var text = current {
                if depth > 0 {
                    if c == "(" { depth += 1 } else if c == ")" { depth -= 1 }
                } else if c == "\\" {
                    let next = line.index(after: i)
                    if next < line.endIndex, line[next] == "(" { depth = 1; text += "X"; i = next }
                    else if next < line.endIndex { text.append(line[next]); i = next }
                } else if c == "\"" {
                    out.append(text); current = nil; i = line.index(after: i); continue
                } else {
                    text.append(c)
                }
                current = text
            } else if c == "\"" {
                current = ""
            }
            i = line.index(after: i)
        }
        return out
    }

    /// Three words or more, a capital first or a full stop last, and no code in it.
    static func readsAsProse(_ text: String) -> Bool {
        let words = text.split(separator: " ")
        guard words.count >= 3 else { return false }
        if text.contains("://") || text.contains("/") || text.contains("_") || text.contains("{") || text.contains("=") { return false }
        let first = text.unicodeScalars.first.map { CharacterSet.uppercaseLetters.contains($0) } ?? false
        let last = text.last.map { ".?!".contains($0) } ?? false
        return first || last
    }

    /// The faults a sentence carries, each named by its rule.
    static func plainFaults(_ text: String) -> [String] {
        var faults: [String] = []
        if text.contains(";") { faults.append("two sentences, not one with a semicolon") }
        if text.contains("—") || text.contains("–") || text.contains(" - ") { faults.append("a comma, a colon or a full stop, never a dash") }
        if text.contains("->") || text.contains("→") || text.contains(" > ") { faults.append("name the place in words, like Settings, Apps") }
        if text.contains("(") || text.contains("[") { faults.append("say it in the sentence, or leave it out") }
        let lower = text.lowercased()
        for word in MECHANISM_WORDS where lower.range(of: "\\b\(word)s?\\b", options: .regularExpression) != nil {
            faults.append("say what the person sees, never how it works inside (\(word))")
        }
        if let first = text.unicodeScalars.first, CharacterSet.lowercaseLetters.contains(first) { faults.append("a sentence starts with a capital") }
        let sentences = text.components(separatedBy: ". ")
        if sentences.contains(where: { $0.split(separator: " ").count > 20 }) { faults.append("one idea per sentence, 20 words at most") }
        return faults
    }

    /// The dashboard's method names (`NativeMethod`, `NativeQuery` and the
    /// saved file's literal in `lib/nativeBridge.ts`) and the shell's dispatch
    /// table are one set: the twin of Android's
    /// `theDispatchTableIsExactlyTheChannelMethods`.
    func testTheDispatchTableIsExactlyTheChannelMethods() throws {
        let bridge = try String(contentsOf: Self.ios.deletingLastPathComponent().appendingPathComponent("src/lib/nativeBridge.ts"), encoding: .utf8)
        var expected = Set<String>()
        for union in ["export type NativeMethod =", "export type NativeQuery ="] {
            let block = try Self.block(of: bridge, from: union, to: "\n\n")
            expected.formUnion(try Self.names(in: block, pattern: "'(\\w+)'"))
        }
        XCTAssertTrue(bridge.contains("{ m: 'saveImageFromBase64', a: [name, mime] }"), "the saved file's own literal")
        expected.insert("saveImageFromBase64")
        XCTAssertEqual(24, expected.count)

        let shell = try text("App/App/ShellViewController.swift")
        let table = try Self.block(of: shell, from: "func dispatchNative(", to: "\n    }\n")
        XCTAssertEqual(expected, try Self.names(in: table, pattern: "case \"(\\w+)\":"))
    }

    /// The text after `start`, up to the first `end` after it.
    private static func block(of text: String, from start: String, to end: String) throws -> String {
        let from = try XCTUnwrap(text.range(of: start)).upperBound
        let rest = text[from...]
        let to = try XCTUnwrap(rest.range(of: end)).lowerBound
        return String(rest[..<to])
    }

    /// The first capture of every match of `pattern` in `text`.
    private static func names(in text: String, pattern: String) throws -> Set<String> {
        let regex = try NSRegularExpression(pattern: pattern)
        let matches = regex.matches(in: text, range: NSRange(text.startIndex..., in: text))
        return Set(matches.compactMap { Range($0.range(at: 1), in: text).map { String(text[$0]) } })
    }

    /// The example Firebase file has a valid shape, so saved under the real
    /// name it would configure Firebase with no push. The config phase takes
    /// it for no file: a Release build stops, a Debug build runs without push,
    /// and testflight.sh asks the phase the same before it builds anything.
    func testTheExampleFirebaseFileNeverReachesAStoreBuild() throws {
        let script = Self.ios.appendingPathComponent("App/firebase-config.sh")
        let example = try Data(contentsOf: Self.app.appendingPathComponent("GoogleService-Info.plist.example"))
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("otodock-firebase-" + UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let source = root.appendingPathComponent("App")
        let bundle = root.appendingPathComponent("build/App.app")
        try FileManager.default.createDirectory(at: source, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: bundle, withIntermediateDirectories: true)
        let plist = source.appendingPathComponent("GoogleService-Info.plist")
        let copied = bundle.appendingPathComponent("GoogleService-Info.plist")
        func phase(_ configuration: String) throws -> (status: Int32, out: String, err: String) {
            return try Self.run(script, [], environment: [
                "SRCROOT": root.path, "TARGET_BUILD_DIR": root.appendingPathComponent("build").path,
                "UNLOCALIZED_RESOURCES_FOLDER_PATH": "App.app", "CONFIGURATION": configuration])
        }

        try example.write(to: plist)
        let release = try phase("Release")
        XCTAssertEqual(1, release.status, "a store build stops on the example")
        XCTAssertTrue(release.err.contains("example file"), release.err)
        let debug = try phase("Debug")
        XCTAssertEqual(0, debug.status, debug.err)
        XCTAssertTrue(debug.out.contains("runs without push"), debug.out)
        XCTAssertFalse(FileManager.default.fileExists(atPath: copied.path), "the example is never copied")
        XCTAssertEqual(1, try Self.run(script, ["--check", plist.path]).status, "testflight.sh's check refuses it")
        XCTAssertEqual(1, try Self.run(script, ["--check", root.appendingPathComponent("missing.plist").path]).status)

        var real = try XCTUnwrap(PropertyListSerialization.propertyList(from: example, format: nil) as? [String: Any])
        real["API_KEY"] = "AIzaTestKeyOfTheShellTest"
        real["GOOGLE_APP_ID"] = "1:236000000001:ios:abcdef0123456789"
        try PropertyListSerialization.data(fromPropertyList: real, format: .xml, options: 0).write(to: plist)
        let store = try phase("Release")
        XCTAssertEqual(0, store.status, store.err)
        XCTAssertTrue(FileManager.default.fileExists(atPath: copied.path), "a real config is copied")
        XCTAssertEqual(0, try Self.run(script, ["--check", plist.path]).status)

        let testflight = try text("testflight.sh")
        XCTAssertTrue(testflight.contains("firebase-config.sh\" --check "), "the upload asks the phase first")
        XCTAssertEqual(2, testflight.components(separatedBy: "2>&1 | redact | tee").count - 1,
                       "both xcodebuild logs go through the redaction")
        XCTAssertTrue(testflight.contains("s|$OTODOCK_IOS_API_KEY_PATH|<key path>|g"), "the .p8 path never lands in a log")
    }

    /// A script run to its end, with its exit status and both streams.
    private static func run(_ script: URL, _ arguments: [String], environment: [String: String] = [:]) throws -> (status: Int32, out: String, err: String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [script.path] + arguments
        var env = ProcessInfo.processInfo.environment
        for (key, value) in environment { env[key] = value }
        process.environment = env
        let out = Pipe()
        let err = Pipe()
        process.standardOutput = out
        process.standardError = err
        try process.run()
        let outData = out.fileHandleForReading.readDataToEndOfFile()
        let errData = err.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return (process.terminationStatus, String(decoding: outData, as: UTF8.self), String(decoding: errData, as: UTF8.self))
    }

    /// The store build signs with the Apple Distribution certificate and the
    /// App Store profile the API key created: the project's Release
    /// configuration names them (Xcode's automatic signing archives with a
    /// development profile, which needs a registered device), Debug stays
    /// automatic for the simulator, and the export names the same pair.
    func testTheStoreBuildSignsWithTheDistributionCertificateAndTheAppStoreProfile() throws {
        let project = try text("App/App.xcodeproj/project.pbxproj")
        let release = try Self.block(of: project, from: "504EC3181FED79650016851F /* Release */ = {", to: "name = Release;")
        XCTAssertTrue(release.contains("CODE_SIGN_STYLE = Manual;"))
        XCTAssertTrue(release.contains("CODE_SIGN_IDENTITY = \"Apple Distribution\";"))
        XCTAssertTrue(release.contains("PROVISIONING_PROFILE_SPECIFIER = \"OtoDock App Store\";"))
        let debug = try Self.block(of: project, from: "504EC3171FED79650016851F /* Debug */ = {", to: "name = Debug;")
        XCTAssertTrue(debug.contains("CODE_SIGN_STYLE = Automatic;"), "the simulator builds keep automatic signing")
        XCTAssertFalse(debug.contains("PROVISIONING_PROFILE_SPECIFIER"))
        let export = try text("App/ExportOptions.plist")
        XCTAssertTrue(export.contains("<key>signingStyle</key>\n\t<string>manual</string>"))
        XCTAssertTrue(export.contains("<key>signingCertificate</key>\n\t<string>Apple Distribution</string>"))
        XCTAssertTrue(export.contains("<key>com.otodock</key>\n\t\t<string>OtoDock App Store</string>"))
        XCTAssertTrue(export.contains("<key>manageAppVersionAndBuildNumber</key>\n\t<false/>"), "the version phase's build number stays")
    }

    /// A build without the Firebase config refuses the registration instead
    /// of ending the app, and never configures Firebase: the twin of
    /// Android's `PushUnconfiguredPluginTest`.
    func testAPushRegistrationWithoutTheFirebaseConfigIsRefusedNotCrashed() throws {
        let push = try text("App/App/PushBridge.swift")
        XCTAssertTrue(push.contains("if Bundle.main.url(forResource: \"GoogleService-Info\", withExtension: \"plist\") != nil {"))
        XCTAssertEqual(1, push.components(separatedBy: "FirebaseApp.configure()").count - 1, "configured once, inside that check")
        let token = try XCTUnwrap(push.range(of: "func apnsTokenArrived(")).upperBound
        let body = String(push[token...].prefix(400))
        XCTAssertTrue(body.contains("guard configured else {"), "the token hand-over checks the config first")
        XCTAssertTrue(body.contains("registrationFailed(NSError("), "a refusal is a registration failure the plugin reports")
        XCTAssertTrue(push.contains("static let notConfigured = \"Push is not configured in this build of the app\""))
        XCTAssertFalse(push.contains("fatalError"))
    }

    func testTheShimNamesTheTwoObjectsAndTheOneHandler() throws {
        let shim = try String(contentsOf: Self.app.appendingPathComponent("OtoDockNative.js"), encoding: .utf8)
        XCTAssertTrue(shim.hasPrefix("// The iOS app's channel"))
        XCTAssertTrue(shim.contains("(function (activeOrigin) {"), "the app calls it with the active origin")
        XCTAssertTrue(shim.trimmingCharacters(in: .whitespacesAndNewlines).hasSuffix("})"), "the file does not call itself")
        XCTAssertTrue(shim.contains("location.origin === activeOrigin"), "the object exists on the active origin's pages only")
        let shell = try text("App/App/ShellViewController.swift")
        XCTAssertTrue(shell.contains("let source = shim + \"(\" + Self.jsQuote(activeOrigin ?? \"\") + \");\""))
        XCTAssertTrue(shim.contains("window.webkit.messageHandlers"))
        XCTAssertTrue(shim.contains("handlers.OtoDockNative"))
        XCTAssertTrue(shim.contains("Object.defineProperty(window, 'OtoDockNative'"))
        XCTAssertTrue(shim.contains("window.Android = {"))
        XCTAssertTrue(shim.contains("location.protocol === 'capacitor:'"), "the setup object only on the bundled page")
        XCTAssertFalse(shim.contains("messageHandlers.bridge"), "the shim never touches Capacitor's handler")
        let handlers = shim.components(separatedBy: "messageHandlers").count - 1
        XCTAssertEqual(1, handlers, "one handler")
    }

    func testTheFourSoundsAreInTheBundleUnderTheNamesTheSenderUses() throws {
        for name in ["notif_success", "notif_warning", "notif_danger", "notif_turn_done"] {
            let url = Self.app.appendingPathComponent("Sounds/\(name).caf")
            XCTAssertTrue(FileManager.default.fileExists(atPath: url.path), name)
            let size = try FileManager.default.attributesOfItem(atPath: url.path)[.size] as? Int ?? 0
            XCTAssertLessThan(size, 2_000_000, "\(name) stays under the 30 s cap")
        }
    }

    func testTheProjectFileHasThePhasesTheSettingsAndTheMembers() throws {
        let project = try text("App/App.xcodeproj/project.pbxproj")
        XCTAssertTrue(project.contains("name = \"Firebase config\";"))
        XCTAssertTrue(project.contains("name = \"OtoDock version\";"))
        XCTAssertTrue(project.contains("shellScript = \"\\\"$SRCROOT/version.sh\\\"\\n\";"))
        XCTAssertTrue(project.contains("ENABLE_USER_SCRIPT_SANDBOXING = NO;"), "the version phase edits the built Info.plist")
        XCTAssertTrue(project.contains("\"$(TARGET_BUILD_DIR)/$(INFOPLIST_PATH)\","),
                      "the built Info.plist is an input of the version phase, which orders it after Xcode writes the file")
        XCTAssertTrue(project.contains("IPHONEOS_DEPLOYMENT_TARGET = 16.0;"))
        XCTAssertTrue(project.contains("PRODUCT_BUNDLE_IDENTIFIER = com.otodock;"))
        XCTAssertTrue(project.contains("CODE_SIGN_ENTITLEMENTS = App/App.entitlements;"))
        XCTAssertTrue(project.contains("TARGETED_DEVICE_FAMILY = \"1,2\";"), "iPad and iPhone")
        XCTAssertTrue(project.contains("productName = FirebaseMessaging;"))
        XCTAssertTrue(project.contains("productName = OtoDockShell;"))
        XCTAssertFalse(project.contains("GoogleService-Info.plist in Resources"), "the Firebase file is copied by the phase, never a member")
        XCTAssertFalse(project.contains("GoogleService-Info.plist.example in Resources"))
        XCTAssertFalse(project.contains("Main.storyboard"))
        for sound in ["notif_success", "notif_warning", "notif_danger", "notif_turn_done"] {
            XCTAssertTrue(project.contains("\(sound).caf in Resources"), sound)
        }
        XCTAssertTrue(project.contains("OtoDockNative.js in Resources"))
        XCTAssertTrue(project.contains("PrivacyInfo.xcprivacy in Resources"))
    }

    func testTheCapacitorConfigKeepsTheTwoPluginsOutAndTheNotificationDelegateOurs() throws {
        let config = try String(contentsOf: Self.ios.deletingLastPathComponent().appendingPathComponent("capacitor.config.ts"), encoding: .utf8)
        let iosBlock = try XCTUnwrap(config.range(of: "  ios: {").map { String(config[$0.lowerBound...]) })
        XCTAssertTrue(iosBlock.contains("handleApplicationNotifications: false"))
        XCTAssertTrue(iosBlock.contains("includePlugins: ["))
        let include = try XCTUnwrap(iosBlock.range(of: "includePlugins: [").map { String(iosBlock[$0.upperBound...]) })
        let list = String(include[..<(include.range(of: "]")?.lowerBound ?? include.endIndex)])
        XCTAssertFalse(list.contains("@capacitor/status-bar"), "the status bar plugin resizes the web view to the window")
        XCTAssertFalse(list.contains("@capacitor/splash-screen"), "the shell owns the launch picture")
        for plugin in ["@capacitor/browser", "@capacitor/geolocation", "@capacitor/push-notifications",
                       "@capacitor-community/text-to-speech", "@capgo/capacitor-speech-recognition"] {
            XCTAssertTrue(list.contains(plugin), plugin)
        }
        XCTAssertTrue(config.contains("presentationOptions: ['banner', 'list', 'sound']"))
    }

    /// Capacitor makes the web view inspectable when the Info.plist's
    /// `CAPACITOR_DEBUG` reads "true": it comes from `debug.xcconfig`, which
    /// only the Debug configurations reference, so a Release build is not
    /// inspectable (the twin of Android's `webContentsDebuggingEnabled: false`).
    func testTheWebViewIsInspectableInDebugBuildsOnly() throws {
        XCTAssertEqual("CAPACITOR_DEBUG = true\n", try text("debug.xcconfig"))
        XCTAssertEqual("$(CAPACITOR_DEBUG)", try plist("Info.plist")["CAPACITOR_DEBUG"] as? String)
        let project = try text("App/App.xcodeproj/project.pbxproj")
        let name = try NSRegularExpression(pattern: "\\n\\t\\t\\tname = (\\w+);")
        var debug = 0
        var release = 0
        for block in project.components(separatedBy: "isa = XCBuildConfiguration;").dropFirst() {
            let match = try XCTUnwrap(name.firstMatch(in: block, range: NSRange(block.startIndex..., in: block)))
            let configuration = String(block[Range(match.range(at: 1), in: block)!])
            let body = String(block[..<Range(match.range, in: block)!.lowerBound])
            if configuration == "Debug" {
                XCTAssertTrue(body.contains("debug.xcconfig"), "every Debug configuration reads the file")
                debug += 1
            } else {
                XCTAssertEqual("Release", configuration)
                XCTAssertFalse(body.contains("baseConfigurationReference"), "no xcconfig on a Release configuration")
                XCTAssertFalse(body.contains("CAPACITOR_DEBUG"))
                release += 1
            }
        }
        XCTAssertEqual(2, debug, "the project's and the target's")
        XCTAssertEqual(2, release)
        let config = try String(contentsOf: Self.ios.deletingLastPathComponent().appendingPathComponent("capacitor.config.ts"), encoding: .utf8)
        let iosBlock = try XCTUnwrap(config.range(of: "  ios: {").map { String(config[$0.lowerBound...]) })
        XCTAssertFalse(iosBlock.contains("webContentsDebuggingEnabled"), "the config never forces inspection on")
    }

    func testTheIgnoreFileKeepsTheFirebaseConfigOut() throws {
        let ignore = try String(contentsOf: Self.ios.deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent(".gitignore"), encoding: .utf8)
        XCTAssertTrue(ignore.contains("dashboard/ios/App/App/GoogleService-Info.plist\n"))
        let template = try text(".gitignore")
        XCTAssertTrue(template.contains("App/App/public"))
        XCTAssertTrue(template.contains("App/App/capacitor.config.json"))
        let example = try plist("GoogleService-Info.plist.example")
        XCTAssertEqual("com.otodock", example["BUNDLE_ID"] as? String)
        XCTAssertFalse((example["API_KEY"] as? String ?? "").hasPrefix("AIza"), "a placeholder, never a key")
    }
}
