import XCTest
@testable import OtoDockShell

/// The judge's decisions, one sender shape at a time: the twins of the
/// Android `NativeBridgeScopeTest` and `NativeBridgeFrameScopeTest` cases,
/// run against the rule itself instead of a device.
final class ChannelJudgeTests: XCTestCase {

    private let active = "https://agents.example.com"
    private let dashboard = "https://agents.example.com/chat/pa/1"
    private let setup = "capacitor://localhost/setup.html"
    private let call = "{\"m\":\"dashboardReady\",\"a\":[]}"

    private func sender(_ isMainFrame: Bool = true, origin: String = "https://agents.example.com",
                        page: String? = "https://agents.example.com/chat/pa/1") -> ChannelJudge.Sender {
        return ChannelJudge.Sender(isMainFrame: isMainFrame, origin: origin, pageURL: page)
    }

    // MARK: - the dashboard channel

    func testTheTopDocumentOfTheActiveOriginReachesTheChannel() {
        XCTAssertEqual(.dashboard, ChannelJudge.judgeChannel(sender(), body: call, activeOrigin: active))
        XCTAssertEqual(.dashboard, ChannelJudge.judgeChannel(sender(origin: "https://AGENTS.example.com:443"), body: call, activeOrigin: active),
                       "case and the default port compare equal")
    }

    func testAChildFrameIsRefusedWhateverItsOrigin() {
        // A sandboxed artifact frame (an opaque origin), a same-origin child
        // frame on its own object, a cross-origin frame: none is the main frame.
        for origin in ["null", active, "https://other.example.net"] {
            XCTAssertEqual(.refused("a child frame"), ChannelJudge.judgeChannel(sender(false, origin: origin), body: call, activeOrigin: active), origin)
        }
    }

    func testAnotherOriginIsRefusedOnTheMainFrame() {
        // The forward-auth sibling, another site, the same host on http.
        for origin in ["https://auth.example.com", "https://evil.example.net", "http://agents.example.com"] {
            XCTAssertEqual(.refused("another origin"),
                           ChannelJudge.judgeChannel(sender(origin: origin, page: origin + "/"), body: call, activeOrigin: active), origin)
        }
        XCTAssertEqual(.refused("another origin"), ChannelJudge.judgeChannel(sender(), body: call, activeOrigin: nil), "no active install")
    }

    func testABodyThatIsNotTextOrIsOversizedIsRefused() {
        XCTAssertEqual(.refused("a non-text message"), ChannelJudge.judgeChannel(sender(), body: ["m": "dashboardReady"], activeOrigin: active))
        XCTAssertEqual(.refused("a non-text message"), ChannelJudge.judgeChannel(sender(), body: nil, activeOrigin: active))
        let oversized = String(repeating: "x", count: NativeBridgePolicy.maxMessageChars + 1)
        XCTAssertEqual(.refused("an oversized message"), ChannelJudge.judgeChannel(sender(), body: oversized, activeOrigin: active))
        let atTheCap = String(repeating: "x", count: NativeBridgePolicy.maxMessageChars)
        XCTAssertEqual(.dashboard, ChannelJudge.judgeChannel(sender(), body: atTheCap, activeOrigin: active))
    }

    func testTheCapCountsUtf16Units() {
        // One astral character is two UTF-16 units and one grapheme.
        let units = NativeBridgePolicy.maxMessageChars / 2 + 1
        let astral = String(repeating: "\u{1F600}", count: units)
        XCTAssertEqual(units, astral.count)
        XCTAssertEqual(.refused("an oversized message"), ChannelJudge.judgeChannel(sender(), body: astral, activeOrigin: active))
    }

    func testNoDashboardPageLivesUnderTheApiPrefix() {
        XCTAssertEqual(.refused("a page outside the dashboard"),
                       ChannelJudge.judgeChannel(sender(page: active + "/v1/files/x.html"), body: call, activeOrigin: active))
        XCTAssertEqual(.dashboard, ChannelJudge.judgeChannel(sender(page: active + "/v1x"), body: call, activeOrigin: active))
    }

    func testTheSetupPageReachesOnlyItsOwnMethodsAndTheDashboardNeverThem() {
        let local = sender(origin: ChannelJudge.localOrigin, page: setup)
        XCTAssertEqual(.setup, ChannelJudge.judgeChannel(local, body: "{\"m\":\"setup.addInstallation\",\"a\":[\"https://x\"]}", activeOrigin: active))
        XCTAssertEqual(.setup, ChannelJudge.judgeChannel(sender(origin: ChannelJudge.localOrigin, page: setup + "?mode=add"), body: call, activeOrigin: nil),
                       "the setup page needs no active install")
        XCTAssertEqual(.refused("a local page that is not the setup page"),
                       ChannelJudge.judgeChannel(sender(origin: ChannelJudge.localOrigin, page: "capacitor://localhost/index.html"), body: call, activeOrigin: active))
        XCTAssertEqual(.refused("a setup method from the dashboard"),
                       ChannelJudge.judgeChannel(sender(), body: "{\"m\":\"setup.switchFromSetup\",\"a\":[\"x\"]}", activeOrigin: active))
        // A server's own /setup.html is a dashboard page of the active origin, not the bundled one.
        XCTAssertEqual(.dashboard, ChannelJudge.judgeChannel(sender(page: active + "/setup.html"), body: call, activeOrigin: active))
    }

    func testTheOrderOfTheChecksIsTheAndroidOne() {
        // The frame, the origin, the type, the size, the page: a child frame is
        // refused before its body is read, another origin before its body's
        // type, and the setup page's body is checked like the dashboard's.
        XCTAssertEqual(.refused("a child frame"), ChannelJudge.judgeChannel(sender(false), body: 42, activeOrigin: active))
        XCTAssertEqual(.refused("another origin"), ChannelJudge.judgeChannel(sender(origin: "https://evil.example.net"), body: 42, activeOrigin: active))
        XCTAssertEqual(.refused("a non-text message"), ChannelJudge.judgeChannel(sender(), body: 42, activeOrigin: active))
        XCTAssertEqual(.refused("a non-text message"),
                       ChannelJudge.judgeChannel(sender(origin: ChannelJudge.localOrigin, page: setup), body: 42, activeOrigin: nil))
        XCTAssertEqual(.refused("an oversized message"),
                       ChannelJudge.judgeChannel(sender(origin: ChannelJudge.localOrigin, page: setup),
                                                 body: String(repeating: "x", count: NativeBridgePolicy.maxMessageChars + 1), activeOrigin: nil))
    }

    // MARK: - Capacitor's handler

    func testThePluginBridgeAnswersTheActiveMainFrameAndRefusesTheThreePlugins() {
        XCTAssertEqual(.forwarded, ChannelJudge.judgeBridge(sender(), pluginId: "Geolocation", activeOrigin: active))
        for plugin in ["CapacitorHttp", "CapacitorCookies", "WebView"] {
            XCTAssertEqual(.refused("the \(plugin) plugin"), ChannelJudge.judgeBridge(sender(), pluginId: plugin, activeOrigin: active))
        }
        XCTAssertEqual(.refused("a child frame"), ChannelJudge.judgeBridge(sender(false), pluginId: "Geolocation", activeOrigin: active))
        XCTAssertEqual(.refused("another origin"),
                       ChannelJudge.judgeBridge(sender(origin: "https://auth.example.com", page: "https://auth.example.com/"), pluginId: "Geolocation", activeOrigin: active))
        XCTAssertEqual(.refused("another origin"), ChannelJudge.judgeBridge(sender(), pluginId: "Geolocation", activeOrigin: nil))
    }

    func testTheSetupPageReachesTheConsoleOnly() {
        let local = sender(origin: ChannelJudge.localOrigin, page: setup)
        XCTAssertEqual(.forwarded, ChannelJudge.judgeBridge(local, pluginId: "Console", activeOrigin: active))
        XCTAssertEqual(.refused("the setup page"), ChannelJudge.judgeBridge(local, pluginId: "Geolocation", activeOrigin: active))
        XCTAssertEqual(.refused("the setup page"),
                       ChannelJudge.judgeBridge(sender(origin: ChannelJudge.localOrigin, page: "capacitor://localhost/index.html"), pluginId: "Console", activeOrigin: active))
        XCTAssertEqual(.refused("the CapacitorCookies plugin"), ChannelJudge.judgeBridge(local, pluginId: "CapacitorCookies", activeOrigin: active),
                       "the refused plugins go first, on every page")
    }

    // MARK: - the prompt channel

    func testTheCookieAndHttpPromptsAreAnsweredEmptyAndEveryOtherPromptIsThePages() {
        XCTAssertEqual("", ChannelJudge.promptAnswer("{\"type\":\"CapacitorCookies.get\",\"url\":\"https://x\"}"))
        XCTAssertEqual("", ChannelJudge.promptAnswer("{\"type\":\"CapacitorCookies.set\",\"action\":\"a=b\"}"))
        XCTAssertEqual("false", ChannelJudge.promptAnswer("{\"type\":\"CapacitorCookies.isEnabled\"}"))
        XCTAssertEqual("false", ChannelJudge.promptAnswer("{\"type\":\"CapacitorHttp\"}"))
        XCTAssertNil(ChannelJudge.promptAnswer("Your name?"))
        XCTAssertNil(ChannelJudge.promptAnswer("{\"type\":\"Other\"}"))
        XCTAssertNil(ChannelJudge.promptAnswer("[1,2]"))
        XCTAssertNil(ChannelJudge.promptAnswer(""))
    }

    // MARK: - origins and pages

    func testAnOriginIsReassembledAsThePolicyWritesIt() {
        XCTAssertEqual("https://agents.example.com", ChannelJudge.originString(protocol: "https", host: "agents.example.com", port: 0))
        XCTAssertEqual("http://192.168.1.5:8400", ChannelJudge.originString(protocol: "http", host: "192.168.1.5", port: 8400))
        XCTAssertEqual("http://[fd00::1]:8400", ChannelJudge.originString(protocol: "http", host: "fd00::1", port: 8400), "WebKit leaves the brackets off")
        XCTAssertEqual("http://[::1]", ChannelJudge.originString(protocol: "http", host: "[::1]", port: 0), "already bracketed")
        XCTAssertEqual("capacitor://localhost", ChannelJudge.originString(protocol: "capacitor", host: "localhost", port: 0))
    }

    func testTheSetupPageIsTheBundledOneOnly() {
        XCTAssertTrue(ChannelJudge.isSetupPage(setup))
        XCTAssertTrue(ChannelJudge.isSetupPage("CAPACITOR://LOCALHOST/setup.html?mode=add#x"))
        XCTAssertFalse(ChannelJudge.isSetupPage("https://agents.example.com/setup.html"), "a server's own page")
        XCTAssertFalse(ChannelJudge.isSetupPage("capacitor://localhost/index.html"))
        XCTAssertFalse(ChannelJudge.isSetupPage("capacitor://localhost/setup.html/x"))
        XCTAssertFalse(ChannelJudge.isSetupPage("capacitor://evil/setup.html"))
        XCTAssertFalse(ChannelJudge.isSetupPage(nil))
    }

    func testOnlyTheUnreachablePageShowsThePicker() {
        XCTAssertTrue(ChannelJudge.isUnreachablePage(setup + "?error=unreachable&url=https%3A%2F%2Fx"))
        XCTAssertTrue(ChannelJudge.isUnreachablePage(setup + "?url=x&error=unreachable"))
        XCTAssertFalse(ChannelJudge.isUnreachablePage(setup + "?mode=add"), "the add screen")
        XCTAssertFalse(ChannelJudge.isUnreachablePage(setup), "the first run")
        XCTAssertFalse(ChannelJudge.isUnreachablePage(setup + "?error=unreachablex"))
        XCTAssertFalse(ChannelJudge.isUnreachablePage("https://agents.example.com/setup.html?error=unreachable"), "a server's page")
    }
}
