import XCTest
@testable import OtoDockShell

/// The native bridge's rules, the port of the Android app's
/// `NativeBridgePolicyTest`, case for case.
final class NativeBridgePolicyTests: XCTestCase {

    typealias P = NativeBridgePolicy

    // MARK: - origins

    func testAnOriginIsSchemeHostAndANonDefaultPort() {
        XCTAssertEqual("https://agents.example.com", P.origin("https://Agents.Example.com"))
        XCTAssertEqual("https://agents.example.com", P.origin("https://agents.example.com:443/chat/x?y=1"))
        XCTAssertEqual("http://192.168.1.10:8400", P.origin("http://192.168.1.10:8400"))
        XCTAssertEqual("http://nas.local", P.origin("http://nas.local:80"))
        XCTAssertEqual("https://xn--bcher-kva.example", P.origin("https://bücher.example"))
    }

    func testANonAsciiHostKeepsItsPort() {
        XCTAssertEqual("https://xn--bcher-kva.example:8443", P.origin("https://bücher.example:8443/chat"))
        XCTAssertEqual("https://xn--bcher-kva.example", P.origin("https://bücher.example:443"))
        XCTAssertEqual("xn--bcher-kva.example", P.host("https://xn--bcher-kva.example:8443"))
        XCTAssertNil(P.origin("https://bücher.example:99999"))
    }

    func testAPortPast65535IsNoOriginOnAnyHost() {
        XCTAssertNil(P.origin("https://x.example:65536"))
        XCTAssertNil(P.origin("http://10.0.0.5:99999"))
        XCTAssertEqual("https://x.example:65535", P.origin("https://x.example:65535"))
    }

    func testAnUnderscoreHostIsAnOrigin() {
        XCTAssertEqual("http://nas_box.local:8400", P.origin("http://nas_box.local:8400"))
        XCTAssertEqual("https://my_server.example.com", P.origin("https://My_Server.example.com/"))
        XCTAssertEqual("nas_box.local", P.host("http://nas_box.local:8400"))
    }

    func testAnIpv6LiteralIsAnOriginInItsCanonicalForm() {
        XCTAssertEqual("https://[2001:db8::1]", P.origin("https://[2001:db8::1]"))
        XCTAssertEqual("https://[2001:db8::1]:8443", P.origin("https://[2001:0DB8:0:0::1]:8443/x"))
        XCTAssertEqual("http://[::1]:8400", P.origin("http://[0:0:0:0:0:0:0:1]:8400"))
        XCTAssertEqual("https://[2001:db8::1:0:0:1]", P.origin("https://[2001:db8:0:0:1:0:0:1]"))
        XCTAssertEqual("https://[::ffff:c000:201]", P.origin("https://[::ffff:192.0.2.1]"))
        XCTAssertEqual("[2001:db8::1]", P.host("https://[2001:db8::1]:8443"))
        XCTAssertTrue(P.sameOrigin("https://[2001:db8::1]", "https://[2001:0db8::0001]:443"))
        XCTAssertNil(P.origin("https://[fe80::1%25wlan0]"), "a zone id is refused")
        XCTAssertNil(P.origin("https://[2001:db8::g]"))
    }

    func testAMalformedIpv6LiteralHasNoOrigin() {
        XCTAssertNil(P.ipv6Literal("[1::2::3]"), "two gaps")
        XCTAssertNil(P.ipv6Literal("[1:2:3:4:5:6:7:8:9]"), "nine pieces")
        XCTAssertNil(P.ipv6Literal("[1:2:3:4:5:6:7]"), "seven pieces and no gap")
        XCTAssertNil(P.ipv6Literal("[1:2:3:4::5:6:7:8]"), "eight pieces and a gap")
        XCTAssertNil(P.ipv6Literal("[2001:db8::00001]"), "a five-digit piece")
        XCTAssertNil(P.ipv6Literal("[::1.2.3.4:5]"), "an IPv4 tail not in last place")
        XCTAssertNil(P.ipv6Literal("[1.2.3.4::]"), "an IPv4 tail before the gap")
        XCTAssertNil(P.ipv6Literal("[::ffff:1.2.3.256]"), "an octet over 255")
        XCTAssertNil(P.ipv6Literal("[::ffff:1.2.3.04]"), "an octet with a leading zero")
        XCTAssertNil(P.ipv6Literal("[::ffff:1.2.3]"), "three octets")
        XCTAssertNil(P.ipv6Literal("[:1:2:3:4:5:6:7]"), "a stray colon")
        XCTAssertNil(P.ipv6Literal("[1:2:3:4:5:6:7:8:]"), "a trailing colon")
        XCTAssertNil(P.ipv6Literal("[]"), "empty")
        XCTAssertNil(P.ipv6Literal("[::1"), "unclosed")
        XCTAssertNil(P.origin("https://[]"))
        XCTAssertNil(P.origin("https://[::1"))
        XCTAssertEqual("[::]", P.ipv6Literal("[0:0:0:0:0:0:0:0]"))
        XCTAssertEqual("[1::]", P.ipv6Literal("[1:0:0:0:0:0:0:0]"))
        XCTAssertEqual("[1:0:2::]", P.ipv6Literal("[1:0:2:0:0:0:0:0]"))
    }

    func testPortZeroIsKeptAsWritten() {
        XCTAssertEqual("https://agents.example.com:0", P.origin("https://agents.example.com:0"))
        XCTAssertEqual("https://[::1]:0", P.origin("https://[::1]:0"))
        XCTAssertEqual("https://xn--bcher-kva.example:0", P.origin("https://bücher.example:0"))
    }

    func testAHostOutsideLettersDigitsDotsHyphensAndUnderscoresHasNoOrigin() {
        XCTAssertNil(P.origin("https://exa$mple.com"))
        XCTAssertNil(P.origin("https://bücher$.example"))
    }

    func testAnAddressThatIsNotAPlainWebOriginHasNone() {
        XCTAssertNil(P.origin(nil))
        XCTAssertNil(P.origin("capacitor://localhost/setup.html"))
        XCTAssertNil(P.origin("https://user:pw@agents.example.com"))
        XCTAssertNil(P.origin("https://agents.example.com\\@other.example"))
        XCTAssertNil(P.origin("otodock://auth/callback"))
        XCTAssertNil(P.origin("not a url"))
    }

    func testSameOriginNormalisesCaseAndDefaultPorts() {
        XCTAssertTrue(P.sameOrigin("https://agents.example.com", "https://AGENTS.example.com:443"))
        XCTAssertTrue(P.sameOrigin("http://192.168.1.10:8400", "http://192.168.1.10:8400"))
        XCTAssertFalse(P.sameOrigin("https://agents.example.com", "https://agents.example.com:8443"))
        XCTAssertFalse(P.sameOrigin("https://agents.example.com", "http://agents.example.com"))
        XCTAssertFalse(P.sameOrigin("https://agents.example.com", "https://auth.example.com"))
        XCTAssertFalse(P.sameOrigin("https://agents.example.com", "null"))
        XCTAssertFalse(P.sameOrigin(nil, "https://agents.example.com"))
    }

    func testNoDashboardPageLivesUnderTheApiPrefix() {
        XCTAssertTrue(P.isDashboardPage("https://agents.example.com/chat/pa/1"))
        XCTAssertTrue(P.isDashboardPage("https://agents.example.com/"))
        XCTAssertFalse(P.isDashboardPage("https://agents.example.com/v1/images/temp/x.html"))
        XCTAssertFalse(P.isDashboardPage(nil))
    }

    func testThePrivacyPolicyIsOtoDocksOwnHttpsPage() {
        XCTAssertEqual("https://otodock.io", P.origin(P.privacyPolicyURL))
        XCTAssertEqual("https://otodock.io/legal/privacy", P.privacyPolicyURL)
    }

    // MARK: - server addresses

    func testPlainHttpReachesOnlyAPrivateIpv4Address() {
        XCTAssertEqual("http://192.168.1.10:8400", P.serverUrl(" http://192.168.1.10:8400/ "))
        XCTAssertEqual("http://10.0.0.5", P.serverUrl("http://10.0.0.5"))
        XCTAssertEqual("http://172.16.0.1:8400", P.serverUrl("http://172.16.0.1:8400"))
        XCTAssertEqual("http://172.31.255.254", P.serverUrl("http://172.31.255.254"))
        XCTAssertEqual("http://127.0.0.1:8400", P.serverUrl("http://127.0.0.1:8400"))
        XCTAssertEqual("http://169.254.1.2", P.serverUrl("http://169.254.1.2"))
        XCTAssertEqual("HTTP://192.168.1.10", P.serverUrl("HTTP://192.168.1.10"))
        XCTAssertNil(P.serverUrl("http://172.32.0.1"))
        XCTAssertNil(P.serverUrl("http://8.8.8.8"))
        XCTAssertNil(P.serverUrl("http://100.64.0.1"))
        XCTAssertNil(P.serverUrl("http://agents.example.com"))
    }

    func testAnOctetTheWebViewWouldReadAsOctalIsRefused() {
        XCTAssertNil(P.serverUrl("http://0127.0.0.1:8400"))
        XCTAssertNil(P.serverUrl("http://010.0.0.1"))
        XCTAssertNil(P.serverUrl("http://0172.16.0.1"))
        XCTAssertNil(P.serverUrl("http://192.168.01.10"))
        XCTAssertNil(P.serverUrl("http://+10.0.0.1"))
        XCTAssertNil(P.serverUrl("http://0x7f.0.0.1"))
        XCTAssertNil(P.serverUrl("http://127.1"))
        XCTAssertNil(P.serverUrl("http://2130706433"))
        XCTAssertNil(P.serverUrl("http://192.168.1.256"))
    }

    func testATrailingDotOrAnotherNameIsNoLanAddress() {
        XCTAssertNil(P.serverUrl("http://192.168.1.10.:8400"))
        XCTAssertNil(P.serverUrl("http://localhost.:8400"))
        XCTAssertNil(P.serverUrl("http://10.0.0.5.nip.io"))
        XCTAssertEqual("http://localhost:8400", P.serverUrl("http://localhost:8400"))
        XCTAssertEqual("http://nas.local:8400", P.serverUrl("http://nas.local:8400"))
        XCTAssertEqual("http://nas_box.local:8400", P.serverUrl("http://nas_box.local:8400"))
        XCTAssertEqual("http://NAS.Local", P.serverUrl("http://NAS.Local"))
    }

    func testPlainHttpReachesAPrivateOrLinkLocalIpv6Address() {
        XCTAssertEqual("http://[::1]:8400", P.serverUrl("http://[::1]:8400"))
        XCTAssertEqual("http://[fd00::5]:8400", P.serverUrl("http://[fd00::5]:8400"))
        XCTAssertEqual("http://[fc00::1]", P.serverUrl("http://[fc00::1]"))
        XCTAssertEqual("http://[FDFF:ffff::1]", P.serverUrl("http://[FDFF:ffff::1]"))
        XCTAssertEqual("http://[fe80::1]:8400", P.serverUrl("http://[fe80::1]:8400"))
        XCTAssertEqual("http://[febf::1]", P.serverUrl("http://[febf::1]"))
        XCTAssertEqual("http://[::ffff:192.168.1.10]", P.serverUrl("http://[::ffff:192.168.1.10]"))
        XCTAssertEqual("http://[0:0:0:0:0:0:0:1]", P.serverUrl("http://[0:0:0:0:0:0:0:1]"))
    }

    func testPlainHttpNeverReachesAPublicOrUnusableIpv6Address() {
        XCTAssertNil(P.serverUrl("http://[2001:4860:4860::8888]"))
        XCTAssertNil(P.serverUrl("http://[2001:db8::1]:8400"))
        XCTAssertNil(P.serverUrl("http://[fec0::1]"), "fec0::/10 is not link-local")
        XCTAssertNil(P.serverUrl("http://[fe00::1]"), "fe00:: sits below fe80::/10")
        XCTAssertNil(P.serverUrl("http://[fbff::1]"), "fb00:: sits below fc00::/7")
        XCTAssertNil(P.serverUrl("http://[::]"), "the unspecified address")
        XCTAssertNil(P.serverUrl("http://[::10.0.0.1]"), "IPv4-compatible")
        XCTAssertNil(P.serverUrl("http://[::ffff:8.8.8.8]"), "a public IPv4-mapped address")
        XCTAssertNil(P.serverUrl("http://[fe80::1%25wlan0]:8400"), "a zone id")
        XCTAssertNil(P.serverUrl("http://[fd00::g]"))
    }

    func testHttpsReachesAnyHostThatIsAnOrigin() {
        XCTAssertEqual("https://agents.example.com", P.serverUrl("agents.example.com"))
        XCTAssertEqual("https://agents.example.com/x", P.serverUrl("https://agents.example.com/x/"))
        XCTAssertEqual("https://[2001:db8::1]:8443", P.serverUrl("https://[2001:db8::1]:8443"))
        XCTAssertEqual("https://8.8.8.8", P.serverUrl("8.8.8.8"))
        XCTAssertEqual("https://[fd00::5]:8400", P.serverUrl("[fd00::5]:8400"))
    }

    func testAnAddressTheAppCouldNotBootIsNeverSaved() {
        XCTAssertNil(P.serverUrl(nil))
        XCTAssertNil(P.serverUrl("   "))
        XCTAssertNil(P.serverUrl("http://u@10.0.0.5:8400"), "user info")
        XCTAssertNil(P.serverUrl("https://u:p@agents.example.com"), "user info")
        XCTAssertNil(P.serverUrl("http://evil.example\\@10.0.0.5"), "a backslash")
        XCTAssertNil(P.serverUrl("http://10.0.0.5:99999"), "a port past 65535")
        XCTAssertNil(P.serverUrl("ftp://10.0.0.5"), "another scheme")
        XCTAssertNil(P.serverUrl("https://exa$mple.com"))
    }

    func testASavedInstallBootsOnlyUnderTheCurrentRule() {
        XCTAssertTrue(P.bootable("https://8.8.8.8"))
        XCTAssertTrue(P.bootable("http://192.168.1.10:8400"))
        XCTAssertTrue(P.bootable(P.origin("http://[fd00::5]:8400")))
        XCTAssertFalse(P.bootable(P.origin("http://0127.0.0.1:8400")))
        XCTAssertFalse(P.bootable(P.origin("http://192.168.1.10.:8400")))
        XCTAssertFalse(P.bootable("http://agents.example.com"))
        XCTAssertFalse(P.bootable(nil))
    }

    func testTheCleartextRuleReadsTheHostAsTheOriginGivesIt() {
        XCTAssertTrue(P.cleartextHost("[fd00::5]"))
        XCTAssertTrue(P.cleartextHost("[::ffff:c0a8:10a]"))
        XCTAssertFalse(P.cleartextHost("fd00::5"))
        XCTAssertFalse(P.cleartextHost("[]"))
        XCTAssertFalse(P.cleartextHost("["))
        XCTAssertFalse(P.cleartextHost(nil))
    }

    // MARK: - messages

    func testAMessageSplitsAtItsFirstNewline() {
        let plain = P.splitMessage("{\"m\":\"x\"}")
        XCTAssertEqual("{\"m\":\"x\"}", plain.head)
        XCTAssertNil(plain.body)
        let withBody = P.splitMessage("{\"m\":\"s\"}\nQUJD\nREVG")
        XCTAssertEqual("{\"m\":\"s\"}", withBody.head)
        XCTAssertEqual("QUJD\nREVG", withBody.body)
        XCTAssertTrue(Int64(P.maxMessageChars) > 4 * Int64(P.maxSaveBytes) / 3 + 4096)
    }

    // MARK: - saving files

    func testOnlyTheLightboxImageTypesAndDiagnosticsAreSaved() {
        XCTAssertEqual("jpg", P.saveExtension("image/jpeg"))
        XCTAssertEqual("jpg", P.saveExtension(" Image/JPG ; charset=x"))
        XCTAssertEqual("png", P.saveExtension("image/png"))
        XCTAssertEqual("gif", P.saveExtension("image/gif"))
        XCTAssertEqual("webp", P.saveExtension("image/webp"))
        XCTAssertEqual("svg", P.saveExtension("image/svg+xml"))
        XCTAssertEqual("heic", P.saveExtension("image/heic"))
        XCTAssertEqual("heif", P.saveExtension("image/heif"))
        XCTAssertEqual("avif", P.saveExtension("image/avif"))
        XCTAssertEqual("bmp", P.saveExtension("image/bmp"))
        XCTAssertEqual("tiff", P.saveExtension("image/tiff"))
        XCTAssertEqual("json", P.saveExtension("application/json"))
        XCTAssertNil(P.saveExtension("application/vnd.android.package-archive"))
        XCTAssertNil(P.saveExtension("text/html"))
        XCTAssertNil(P.saveExtension(nil))
    }

    func testTheSavedNameTakesItsExtensionFromTheType() {
        XCTAssertEqual("Statement.pdf.png", P.saveFileName("Statement.pdf.apk", "png"))
        XCTAssertEqual("photo.jpg", P.saveFileName("../../photo.jpg", "jpg"))
        XCTAssertEqual("hidden.png", P.saveFileName(".hidden", "png"))
        XCTAssertEqual("a_b_c.json", P.saveFileName("a:b*c.json", "json"))
        XCTAssertEqual("image.png", P.saveFileName("", "png"))
        XCTAssertEqual("image.png", P.saveFileName(nil, "png"))
        let longName = P.saveFileName(String(repeating: "x", count: 300) + ".png", "png")
        XCTAssertEqual(104, longName.count)
    }

    func testTheSavedPayloadIsBounded() {
        XCTAssertTrue(P.saveBase64Fits(4 * 1024))
        XCTAssertTrue(P.saveBase64Fits(33_333_336))
        XCTAssertFalse(P.saveBase64Fits(40_000_000))
    }

    // MARK: - the switcher

    func testALabelIsTrimmedAndCappedAtEightyCharacters() {
        XCTAssertEqual("Site office", P.capLabel("  Site office "))
        XCTAssertNil(P.capLabel("   "))
        XCTAssertNil(P.capLabel(nil))
        let capped = P.capLabel(String(repeating: "a", count: 500))
        XCTAssertEqual(80, capped?.count)
        let emoji = String(repeating: "🏗", count: 90)
        let cappedEmoji = P.capLabel(emoji)
        XCTAssertEqual(160, cappedEmoji?.utf16.count, "never splits a surrogate pair")
        XCTAssertEqual(80, cappedEmoji?.unicodeScalars.count)
    }

    // MARK: - the system browser

    func testOnlyAPlainHttpsAddressOpensTheBrowser() {
        XCTAssertEqual("https://auth.example.com/authorize?x=1", P.authBrowserUrl("https://auth.example.com/authorize?x=1"))
        XCTAssertNil(P.authBrowserUrl("http://auth.example.com/"))
        XCTAssertNil(P.authBrowserUrl("https://user@auth.example.com/"))
        XCTAssertNil(P.authBrowserUrl("https://auth.example.com\\@x.example/"))
        XCTAssertNil(P.authBrowserUrl("intent://x#Intent;end"))
        XCTAssertNil(P.authBrowserUrl(nil))
    }

    func testAnotherSiteAsksFirstAndThePasskeyPageDoesNot() {
        let active = "https://agents.example.com"
        XCTAssertFalse(P.needsHostConfirm("https://Agents.example.com:443/native-passkey?h=n", active))
        XCTAssertTrue(P.needsHostConfirm("https://auth.example.com/authorize", active))
        XCTAssertTrue(P.needsHostConfirm("https://agents.example.com:8443/x", active))
        XCTAssertTrue(P.needsHostConfirm("https://accounts.google.com/o/oauth2", active))
    }

    func testAFlowIsAuthOrOauth() {
        XCTAssertTrue(P.isFlowKind("auth"))
        XCTAssertTrue(P.isFlowKind("oauth"))
        XCTAssertFalse(P.isFlowKind("other"))
        XCTAssertFalse(P.isFlowKind(nil))
    }

    // MARK: - the deep link

    private let now: Int64 = 5_000_000
    private var soon: Int64 { now + 60_000 }

    func testALinkIsTakenOnlyWhileAFlowOfItsKindIsInFlight() {
        XCTAssertEqual("i1", P.deepLinkTarget("auth", "auth", soon, now, "i1", nil))
        XCTAssertEqual("i1", P.deepLinkTarget("oauth", "oauth", soon, now, "i1", nil))
        XCTAssertNil(P.deepLinkTarget("auth", nil, 0, now, nil, nil), "no flow")
        XCTAssertNil(P.deepLinkTarget("auth", "oauth", soon, now, "i1", nil), "wrong kind")
        XCTAssertNil(P.deepLinkTarget("auth", "auth", now - 1, now, "i1", nil), "expired")
        XCTAssertNil(P.deepLinkTarget("auth", "auth", now + P.authFlowTTLMs + 1, now, "i1", nil),
                     "too far ahead (a reboot restarted the clock)")
        XCTAssertNil(P.deepLinkTarget("auth", "auth", soon, now, nil, nil), "no initiating install")
    }

    func testALinkGoesOnlyToTheInstallThatStartedIt() {
        XCTAssertEqual("i1", P.deepLinkTarget("oauth", "oauth", soon, now, "i1", "i1"))
        XCTAssertNil(P.deepLinkTarget("oauth", "oauth", soon, now, "i1", "i2"))
    }
}
