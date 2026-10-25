package com.otodock;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

/** The native bridge's rules, as plain Java. */
public class NativeBridgePolicyTest {

    // --- origins ---

    @Test
    public void anOriginIsSchemeHostAndANonDefaultPort() {
        assertEquals("https://agents.example.com", NativeBridgePolicy.origin("https://Agents.Example.com"));
        assertEquals("https://agents.example.com", NativeBridgePolicy.origin("https://agents.example.com:443/chat/x?y=1"));
        assertEquals("http://192.168.1.10:8400", NativeBridgePolicy.origin("http://192.168.1.10:8400"));
        assertEquals("http://nas.local", NativeBridgePolicy.origin("http://nas.local:80"));
        assertEquals("https://xn--bcher-kva.example", NativeBridgePolicy.origin("https://bücher.example"));
    }

    @Test
    public void aNonAsciiHostKeepsItsPort() {
        assertEquals("https://xn--bcher-kva.example:8443", NativeBridgePolicy.origin("https://bücher.example:8443/chat"));
        assertEquals("https://xn--bcher-kva.example", NativeBridgePolicy.origin("https://bücher.example:443"));
        assertEquals("xn--bcher-kva.example", NativeBridgePolicy.host("https://xn--bcher-kva.example:8443"));
        assertNull(NativeBridgePolicy.origin("https://bücher.example:99999"));
    }

    @Test
    public void aPortPast65535IsNoOriginOnAnyHost() {
        // java.net.URI parses a server host's port with no range check.
        assertNull(NativeBridgePolicy.origin("https://x.example:65536"));
        assertNull(NativeBridgePolicy.origin("http://10.0.0.5:99999"));
        assertEquals("https://x.example:65535", NativeBridgePolicy.origin("https://x.example:65535"));
    }

    @Test
    public void anUnderscoreHostIsAnOrigin() {
        assertEquals("http://nas_box.local:8400", NativeBridgePolicy.origin("http://nas_box.local:8400"));
        assertEquals("https://my_server.example.com", NativeBridgePolicy.origin("https://My_Server.example.com/"));
        assertEquals("nas_box.local", NativeBridgePolicy.host("http://nas_box.local:8400"));
    }

    @Test
    public void anIpv6LiteralIsAnOriginInItsCanonicalForm() {
        assertEquals("https://[2001:db8::1]", NativeBridgePolicy.origin("https://[2001:db8::1]"));
        assertEquals("https://[2001:db8::1]:8443", NativeBridgePolicy.origin("https://[2001:0DB8:0:0::1]:8443/x"));
        assertEquals("http://[::1]:8400", NativeBridgePolicy.origin("http://[0:0:0:0:0:0:0:1]:8400"));
        assertEquals("https://[2001:db8::1:0:0:1]", NativeBridgePolicy.origin("https://[2001:db8:0:0:1:0:0:1]"));
        assertEquals("https://[::ffff:c000:201]", NativeBridgePolicy.origin("https://[::ffff:192.0.2.1]"));
        assertEquals("[2001:db8::1]", NativeBridgePolicy.host("https://[2001:db8::1]:8443"));
        assertTrue(NativeBridgePolicy.sameOrigin("https://[2001:db8::1]", "https://[2001:0db8::0001]:443"));
        assertNull("a zone id is refused", NativeBridgePolicy.origin("https://[fe80::1%25wlan0]"));
        assertNull(NativeBridgePolicy.origin("https://[2001:db8::g]"));
    }

    @Test
    public void aMalformedIpv6LiteralHasNoOrigin() {
        assertNull("two gaps", NativeBridgePolicy.ipv6Literal("[1::2::3]"));
        assertNull("nine pieces", NativeBridgePolicy.ipv6Literal("[1:2:3:4:5:6:7:8:9]"));
        assertNull("seven pieces and no gap", NativeBridgePolicy.ipv6Literal("[1:2:3:4:5:6:7]"));
        assertNull("eight pieces and a gap", NativeBridgePolicy.ipv6Literal("[1:2:3:4::5:6:7:8]"));
        assertNull("a five-digit piece", NativeBridgePolicy.ipv6Literal("[2001:db8::00001]"));
        assertNull("an IPv4 tail not in last place", NativeBridgePolicy.ipv6Literal("[::1.2.3.4:5]"));
        assertNull("an IPv4 tail before the gap", NativeBridgePolicy.ipv6Literal("[1.2.3.4::]"));
        assertNull("an octet over 255", NativeBridgePolicy.ipv6Literal("[::ffff:1.2.3.256]"));
        assertNull("an octet with a leading zero", NativeBridgePolicy.ipv6Literal("[::ffff:1.2.3.04]"));
        assertNull("three octets", NativeBridgePolicy.ipv6Literal("[::ffff:1.2.3]"));
        assertNull("a stray colon", NativeBridgePolicy.ipv6Literal("[:1:2:3:4:5:6:7]"));
        assertNull("a trailing colon", NativeBridgePolicy.ipv6Literal("[1:2:3:4:5:6:7:8:]"));
        assertNull("empty", NativeBridgePolicy.ipv6Literal("[]"));
        assertNull("unclosed", NativeBridgePolicy.ipv6Literal("[::1"));
        assertNull(NativeBridgePolicy.origin("https://[]"));
        assertNull(NativeBridgePolicy.origin("https://[::1"));
        assertEquals("[::]", NativeBridgePolicy.ipv6Literal("[0:0:0:0:0:0:0:0]"));
        assertEquals("[1::]", NativeBridgePolicy.ipv6Literal("[1:0:0:0:0:0:0:0]"));
        assertEquals("[1:0:2::]", NativeBridgePolicy.ipv6Literal("[1:0:2:0:0:0:0:0]"));
    }

    @Test
    public void portZeroIsKeptAsWritten() {
        assertEquals("https://agents.example.com:0", NativeBridgePolicy.origin("https://agents.example.com:0"));
        assertEquals("https://[::1]:0", NativeBridgePolicy.origin("https://[::1]:0"));
        assertEquals("https://xn--bcher-kva.example:0", NativeBridgePolicy.origin("https://bücher.example:0"));
    }

    @Test
    public void aHostOutsideLettersDigitsDotsHyphensAndUnderscoresHasNoOrigin() {
        assertNull(NativeBridgePolicy.origin("https://exa$mple.com"));
        assertNull(NativeBridgePolicy.origin("https://bücher$.example"));
    }

    @Test
    public void anAddressThatIsNotAPlainWebOriginHasNone() {
        assertNull(NativeBridgePolicy.origin(null));
        assertNull(NativeBridgePolicy.origin("file:///android_asset/public/setup.html"));
        assertNull(NativeBridgePolicy.origin("https://user:pw@agents.example.com"));
        assertNull(NativeBridgePolicy.origin("https://agents.example.com\\@other.example"));
        assertNull(NativeBridgePolicy.origin("otodock://auth/callback"));
        assertNull(NativeBridgePolicy.origin("not a url"));
    }

    @Test
    public void sameOriginNormalisesCaseAndDefaultPorts() {
        assertTrue(NativeBridgePolicy.sameOrigin("https://agents.example.com", "https://AGENTS.example.com:443"));
        assertTrue(NativeBridgePolicy.sameOrigin("http://192.168.1.10:8400", "http://192.168.1.10:8400"));
        assertFalse(NativeBridgePolicy.sameOrigin("https://agents.example.com", "https://agents.example.com:8443"));
        assertFalse(NativeBridgePolicy.sameOrigin("https://agents.example.com", "http://agents.example.com"));
        assertFalse(NativeBridgePolicy.sameOrigin("https://agents.example.com", "https://auth.example.com"));
        assertFalse(NativeBridgePolicy.sameOrigin("https://agents.example.com", "null"));
        assertFalse(NativeBridgePolicy.sameOrigin(null, "https://agents.example.com"));
    }

    @Test
    public void noDashboardPageLivesUnderTheApiPrefix() {
        assertTrue(NativeBridgePolicy.isDashboardPage("https://agents.example.com/chat/pa/1"));
        assertTrue(NativeBridgePolicy.isDashboardPage("https://agents.example.com/"));
        assertFalse(NativeBridgePolicy.isDashboardPage("https://agents.example.com/v1/images/temp/x.html"));
        assertFalse(NativeBridgePolicy.isDashboardPage(null));
    }

    @Test
    public void thePrivacyPolicyIsOtoDocksOwnHttpsPage() {
        assertEquals("https://otodock.io", NativeBridgePolicy.origin(NativeBridgePolicy.PRIVACY_POLICY_URL));
        assertEquals("https://otodock.io/legal/privacy", NativeBridgePolicy.PRIVACY_POLICY_URL);
    }

    // --- server addresses ---

    @Test
    public void plainHttpReachesOnlyAPrivateIpv4Address() {
        assertEquals("http://192.168.1.10:8400", NativeBridgePolicy.serverUrl(" http://192.168.1.10:8400/ "));
        assertEquals("http://10.0.0.5", NativeBridgePolicy.serverUrl("http://10.0.0.5"));
        assertEquals("http://172.16.0.1:8400", NativeBridgePolicy.serverUrl("http://172.16.0.1:8400"));
        assertEquals("http://172.31.255.254", NativeBridgePolicy.serverUrl("http://172.31.255.254"));
        assertEquals("http://127.0.0.1:8400", NativeBridgePolicy.serverUrl("http://127.0.0.1:8400"));
        assertEquals("http://169.254.1.2", NativeBridgePolicy.serverUrl("http://169.254.1.2"));
        assertEquals("HTTP://192.168.1.10", NativeBridgePolicy.serverUrl("HTTP://192.168.1.10"));
        assertNull(NativeBridgePolicy.serverUrl("http://172.32.0.1"));
        assertNull(NativeBridgePolicy.serverUrl("http://8.8.8.8"));
        assertNull(NativeBridgePolicy.serverUrl("http://100.64.0.1"));
        assertNull(NativeBridgePolicy.serverUrl("http://agents.example.com"));
    }

    @Test
    public void anOctetTheWebViewWouldReadAsOctalIsRefused() {
        // "0127" is 87 to the WebView, "010" is 8 and "0172" is 122.
        assertNull(NativeBridgePolicy.serverUrl("http://0127.0.0.1:8400"));
        assertNull(NativeBridgePolicy.serverUrl("http://010.0.0.1"));
        assertNull(NativeBridgePolicy.serverUrl("http://0172.16.0.1"));
        assertNull(NativeBridgePolicy.serverUrl("http://192.168.01.10"));
        assertNull(NativeBridgePolicy.serverUrl("http://+10.0.0.1"));
        assertNull(NativeBridgePolicy.serverUrl("http://0x7f.0.0.1"));
        assertNull(NativeBridgePolicy.serverUrl("http://127.1"));
        assertNull(NativeBridgePolicy.serverUrl("http://2130706433"));
        assertNull(NativeBridgePolicy.serverUrl("http://192.168.1.256"));
    }

    @Test
    public void aTrailingDotOrAnotherNameIsNoLanAddress() {
        // The WebView drops the dot of an IPv4 address, so the channel's
        // origin would never match the booted one.
        assertNull(NativeBridgePolicy.serverUrl("http://192.168.1.10.:8400"));
        assertNull(NativeBridgePolicy.serverUrl("http://localhost.:8400"));
        assertNull(NativeBridgePolicy.serverUrl("http://10.0.0.5.nip.io"));
        assertEquals("http://localhost:8400", NativeBridgePolicy.serverUrl("http://localhost:8400"));
        assertEquals("http://nas.local:8400", NativeBridgePolicy.serverUrl("http://nas.local:8400"));
        assertEquals("http://nas_box.local:8400", NativeBridgePolicy.serverUrl("http://nas_box.local:8400"));
        assertEquals("http://NAS.Local", NativeBridgePolicy.serverUrl("http://NAS.Local"));
    }

    @Test
    public void plainHttpReachesAPrivateOrLinkLocalIpv6Address() {
        assertEquals("http://[::1]:8400", NativeBridgePolicy.serverUrl("http://[::1]:8400"));
        assertEquals("http://[fd00::5]:8400", NativeBridgePolicy.serverUrl("http://[fd00::5]:8400"));
        assertEquals("http://[fc00::1]", NativeBridgePolicy.serverUrl("http://[fc00::1]"));
        assertEquals("http://[FDFF:ffff::1]", NativeBridgePolicy.serverUrl("http://[FDFF:ffff::1]"));
        assertEquals("http://[fe80::1]:8400", NativeBridgePolicy.serverUrl("http://[fe80::1]:8400"));
        assertEquals("http://[febf::1]", NativeBridgePolicy.serverUrl("http://[febf::1]"));
        assertEquals("http://[::ffff:192.168.1.10]", NativeBridgePolicy.serverUrl("http://[::ffff:192.168.1.10]"));
        assertEquals("http://[0:0:0:0:0:0:0:1]", NativeBridgePolicy.serverUrl("http://[0:0:0:0:0:0:0:1]"));
    }

    @Test
    public void plainHttpNeverReachesAPublicOrUnusableIpv6Address() {
        assertNull(NativeBridgePolicy.serverUrl("http://[2001:4860:4860::8888]"));
        assertNull(NativeBridgePolicy.serverUrl("http://[2001:db8::1]:8400"));
        assertNull("fec0::/10 is not link-local", NativeBridgePolicy.serverUrl("http://[fec0::1]"));
        assertNull("fe00:: sits below fe80::/10", NativeBridgePolicy.serverUrl("http://[fe00::1]"));
        assertNull("fb00:: sits below fc00::/7", NativeBridgePolicy.serverUrl("http://[fbff::1]"));
        assertNull("the unspecified address", NativeBridgePolicy.serverUrl("http://[::]"));
        assertNull("IPv4-compatible", NativeBridgePolicy.serverUrl("http://[::10.0.0.1]"));
        assertNull("a public IPv4-mapped address", NativeBridgePolicy.serverUrl("http://[::ffff:8.8.8.8]"));
        assertNull("a zone id", NativeBridgePolicy.serverUrl("http://[fe80::1%25wlan0]:8400"));
        assertNull(NativeBridgePolicy.serverUrl("http://[fd00::g]"));
    }

    @Test
    public void httpsReachesAnyHostThatIsAnOrigin() {
        assertEquals("https://agents.example.com", NativeBridgePolicy.serverUrl("agents.example.com"));
        assertEquals("https://agents.example.com/x", NativeBridgePolicy.serverUrl("https://agents.example.com/x/"));
        assertEquals("https://[2001:db8::1]:8443", NativeBridgePolicy.serverUrl("https://[2001:db8::1]:8443"));
        assertEquals("https://8.8.8.8", NativeBridgePolicy.serverUrl("8.8.8.8"));
        assertEquals("https://[fd00::5]:8400", NativeBridgePolicy.serverUrl("[fd00::5]:8400"));
    }

    @Test
    public void anAddressTheAppCouldNotBootIsNeverSaved() {
        assertNull(NativeBridgePolicy.serverUrl(null));
        assertNull(NativeBridgePolicy.serverUrl("   "));
        assertNull("user info", NativeBridgePolicy.serverUrl("http://u@10.0.0.5:8400"));
        assertNull("user info", NativeBridgePolicy.serverUrl("https://u:p@agents.example.com"));
        assertNull("a backslash", NativeBridgePolicy.serverUrl("http://evil.example\\@10.0.0.5"));
        assertNull("a port past 65535", NativeBridgePolicy.serverUrl("http://10.0.0.5:99999"));
        assertNull("another scheme", NativeBridgePolicy.serverUrl("ftp://10.0.0.5"));
        assertNull(NativeBridgePolicy.serverUrl("https://exa$mple.com"));
    }

    @Test
    public void aSavedInstallBootsOnlyUnderTheCurrentRule() {
        assertTrue(NativeBridgePolicy.bootable("https://8.8.8.8"));
        assertTrue(NativeBridgePolicy.bootable("http://192.168.1.10:8400"));
        assertTrue(NativeBridgePolicy.bootable(NativeBridgePolicy.origin("http://[fd00::5]:8400")));
        // Saved before the leading-zero refusal: no longer booted in cleartext.
        assertFalse(NativeBridgePolicy.bootable(NativeBridgePolicy.origin("http://0127.0.0.1:8400")));
        assertFalse(NativeBridgePolicy.bootable(NativeBridgePolicy.origin("http://192.168.1.10.:8400")));
        assertFalse(NativeBridgePolicy.bootable("http://agents.example.com"));
        assertFalse(NativeBridgePolicy.bootable(null));
    }

    @Test
    public void theCleartextRuleReadsTheHostAsTheOriginGivesIt() {
        assertTrue(NativeBridgePolicy.cleartextHost("[fd00::5]"));
        assertTrue(NativeBridgePolicy.cleartextHost("[::ffff:c0a8:10a]"));
        assertFalse(NativeBridgePolicy.cleartextHost("fd00::5"));
        assertFalse(NativeBridgePolicy.cleartextHost("[]"));
        assertFalse(NativeBridgePolicy.cleartextHost("["));
        assertFalse(NativeBridgePolicy.cleartextHost(null));
    }

    // --- messages ---

    @Test
    public void aMessageSplitsAtItsFirstNewline() {
        assertArrayEquals(new String[]{"{\"m\":\"x\"}", null}, NativeBridgePolicy.splitMessage("{\"m\":\"x\"}"));
        assertArrayEquals(new String[]{"{\"m\":\"s\"}", "QUJD\nREVG"},
                NativeBridgePolicy.splitMessage("{\"m\":\"s\"}\nQUJD\nREVG"));
        assertTrue(NativeBridgePolicy.MAX_MESSAGE_CHARS > 4L * NativeBridgePolicy.MAX_SAVE_BYTES / 3 + 4096);
    }

    // --- saving files ---

    @Test
    public void onlyTheLightboxImageTypesAndDiagnosticsAreSaved() {
        assertEquals("jpg", NativeBridgePolicy.saveExtension("image/jpeg"));
        assertEquals("jpg", NativeBridgePolicy.saveExtension(" Image/JPG ; charset=x"));
        assertEquals("png", NativeBridgePolicy.saveExtension("image/png"));
        assertEquals("gif", NativeBridgePolicy.saveExtension("image/gif"));
        assertEquals("webp", NativeBridgePolicy.saveExtension("image/webp"));
        assertEquals("svg", NativeBridgePolicy.saveExtension("image/svg+xml"));
        assertEquals("heic", NativeBridgePolicy.saveExtension("image/heic"));
        assertEquals("heif", NativeBridgePolicy.saveExtension("image/heif"));
        assertEquals("avif", NativeBridgePolicy.saveExtension("image/avif"));
        assertEquals("bmp", NativeBridgePolicy.saveExtension("image/bmp"));
        assertEquals("tiff", NativeBridgePolicy.saveExtension("image/tiff"));
        assertEquals("json", NativeBridgePolicy.saveExtension("application/json"));
        assertNull(NativeBridgePolicy.saveExtension("application/vnd.android.package-archive"));
        assertNull(NativeBridgePolicy.saveExtension("text/html"));
        assertNull(NativeBridgePolicy.saveExtension(null));
    }

    @Test
    public void theSavedNameTakesItsExtensionFromTheType() {
        assertEquals("Statement.pdf.png", NativeBridgePolicy.saveFileName("Statement.pdf.apk", "png"));
        assertEquals("photo.jpg", NativeBridgePolicy.saveFileName("../../photo.jpg", "jpg"));
        assertEquals("hidden.png", NativeBridgePolicy.saveFileName(".hidden", "png"));
        assertEquals("a_b_c.json", NativeBridgePolicy.saveFileName("a:b*c.json", "json"));
        assertEquals("image.png", NativeBridgePolicy.saveFileName("", "png"));
        assertEquals("image.png", NativeBridgePolicy.saveFileName(null, "png"));
        String longName = NativeBridgePolicy.saveFileName(new String(new char[300]).replace('\0', 'x') + ".png", "png");
        assertEquals(104, longName.length());
    }

    @Test
    public void theSavedPayloadIsBounded() {
        assertTrue(NativeBridgePolicy.saveBase64Fits(4 * 1024));
        assertTrue(NativeBridgePolicy.saveBase64Fits(33_333_336));
        assertFalse(NativeBridgePolicy.saveBase64Fits(40_000_000));
    }

    // --- the switcher ---

    @Test
    public void aLabelIsTrimmedAndCappedAtEightyCharacters() {
        assertEquals("Site office", NativeBridgePolicy.capLabel("  Site office "));
        assertNull(NativeBridgePolicy.capLabel("   "));
        assertNull(NativeBridgePolicy.capLabel(null));
        String capped = NativeBridgePolicy.capLabel(new String(new char[500]).replace('\0', 'a'));
        assertEquals(80, capped.length());
        String emoji = new String(new char[90]).replace("\0", "🏗");
        String cappedEmoji = NativeBridgePolicy.capLabel(emoji);
        assertEquals("never splits a surrogate pair", 160, cappedEmoji.length());
        assertEquals(80, cappedEmoji.codePointCount(0, cappedEmoji.length()));
    }

    // --- the system browser ---

    @Test
    public void onlyAPlainHttpsAddressOpensTheBrowser() {
        assertEquals("https://auth.example.com/authorize?x=1",
                NativeBridgePolicy.authBrowserUrl("https://auth.example.com/authorize?x=1"));
        assertNull(NativeBridgePolicy.authBrowserUrl("http://auth.example.com/"));
        assertNull(NativeBridgePolicy.authBrowserUrl("https://user@auth.example.com/"));
        assertNull(NativeBridgePolicy.authBrowserUrl("https://auth.example.com\\@x.example/"));
        assertNull(NativeBridgePolicy.authBrowserUrl("intent://x#Intent;end"));
        assertNull(NativeBridgePolicy.authBrowserUrl(null));
    }

    @Test
    public void anotherSiteAsksFirstAndThePasskeyPageDoesNot() {
        String active = "https://agents.example.com";
        assertFalse(NativeBridgePolicy.needsHostConfirm("https://Agents.example.com:443/native-passkey?h=n", active));
        assertTrue(NativeBridgePolicy.needsHostConfirm("https://auth.example.com/authorize", active));
        assertTrue(NativeBridgePolicy.needsHostConfirm("https://agents.example.com:8443/x", active));
        assertTrue(NativeBridgePolicy.needsHostConfirm("https://accounts.google.com/o/oauth2", active));
    }

    @Test
    public void aFlowIsAuthOrOauth() {
        assertTrue(NativeBridgePolicy.isFlowKind("auth"));
        assertTrue(NativeBridgePolicy.isFlowKind("oauth"));
        assertFalse(NativeBridgePolicy.isFlowKind("other"));
        assertFalse(NativeBridgePolicy.isFlowKind(null));
    }

    // --- the deep link ---

    private static final long NOW = 5_000_000L;
    private static final long SOON = NOW + 60_000L;

    @Test
    public void aLinkIsTakenOnlyWhileAFlowOfItsKindIsInFlight() {
        assertEquals("i1", NativeBridgePolicy.deepLinkTarget("auth", "auth", SOON, NOW, "i1", null));
        assertEquals("i1", NativeBridgePolicy.deepLinkTarget("oauth", "oauth", SOON, NOW, "i1", null));
        assertNull("no flow", NativeBridgePolicy.deepLinkTarget("auth", null, 0L, NOW, null, null));
        assertNull("wrong kind", NativeBridgePolicy.deepLinkTarget("auth", "oauth", SOON, NOW, "i1", null));
        assertNull("expired", NativeBridgePolicy.deepLinkTarget("auth", "auth", NOW - 1, NOW, "i1", null));
        assertNull("too far ahead (a reboot restarted the clock)",
                NativeBridgePolicy.deepLinkTarget("auth", "auth", NOW + NativeBridgePolicy.AUTH_FLOW_TTL_MS + 1, NOW, "i1", null));
        assertNull("no initiating install", NativeBridgePolicy.deepLinkTarget("auth", "auth", SOON, NOW, null, null));
    }

    @Test
    public void aLinkGoesOnlyToTheInstallThatStartedIt() {
        assertEquals("i1", NativeBridgePolicy.deepLinkTarget("oauth", "oauth", SOON, NOW, "i1", "i1"));
        assertNull(NativeBridgePolicy.deepLinkTarget("oauth", "oauth", SOON, NOW, "i1", "i2"));
    }
}
