import XCTest
@testable import OtoDockShell

/// The installations registry: the invariants, the public list, the route
/// spent once, first writer wins on the install id, the JSON read back.
final class InstallStoreTests: XCTestCase {

    private func fresh() -> (InstallStore, MemoryBacking) {
        let backing = MemoryBacking()
        return (InstallStore(backing: backing), backing)
    }

    func testTheFirstInstallIsActiveAndTheFavorite() {
        let (store, _) = fresh()
        let a = store.add(url: "https://a.example.com", label: nil)
        XCTAssertEqual("a.example.com", a.label)
        XCTAssertTrue(a.favorite)
        XCTAssertEqual(a.id, store.activeIdValue)
        XCTAssertEqual(a.id, store.favorite()?.id)
    }

    func testASecondInstallOnTheSameHostIsTheExistingOne() {
        let (store, _) = fresh()
        let a = store.add(url: "https://a.example.com", label: "A")
        let again = store.add(url: "https://A.example.com/x", label: "Other")
        XCTAssertEqual(a.id, again.id)
        XCTAssertEqual(1, store.list().count)
    }

    func testExactlyOneFavoriteAndAValidActivePointer() {
        let (store, _) = fresh()
        let a = store.add(url: "https://a.example.com", label: "A")
        let b = store.add(url: "https://b.example.com", label: "B")
        store.setFavorite(b.id)
        XCTAssertEqual([false, true], store.list().map { $0.favorite })
        store.setActive(b.id)
        store.remove(b.id)
        XCTAssertEqual(a.id, store.activeIdValue)
        XCTAssertTrue(store.list()[0].favorite)
        store.remove(a.id)
        XCTAssertNil(store.activeIdValue)
        XCTAssertNil(store.favorite())
    }

    func testTheRegistryReadsBackFromItsJson() {
        let (store, backing) = fresh()
        let a = store.add(url: "https://a.example.com", label: "A")
        store.setLastRoute(a.id, "/chat/pa/1")
        store.setInstallId(a.id, "pid-1")
        let reread = InstallStore(backing: backing)
        let got = reread.active()
        XCTAssertEqual(a.id, got?.id)
        XCTAssertEqual("/chat/pa/1", got?.lastRoute)
        XCTAssertEqual("pid-1", got?.installId)
        XCTAssertEqual("A", got?.label)
    }

    func testAnInstallIdBindsToTheFirstWriter() {
        let (store, _) = fresh()
        let a = store.add(url: "https://a.example.com", label: "A")
        let b = store.add(url: "https://b.example.com", label: "B")
        XCTAssertTrue(store.setInstallId(a.id, "pid"))
        XCTAssertFalse(store.setInstallId(b.id, "pid"))
        XCTAssertEqual(a.id, store.byInstallId("pid")?.id)
        XCTAssertNil(store.byInstallId("other"))
        XCTAssertNil(store.byInstallId(nil))
    }

    func testARenameIsCappedAndAnEmptyOneIgnored() {
        let (store, _) = fresh()
        let a = store.add(url: "https://a.example.com", label: "A")
        store.rename(a.id, "  Site office ")
        XCTAssertEqual("Site office", store.byId(a.id)?.label)
        store.rename(a.id, "   ")
        XCTAssertEqual("Site office", store.byId(a.id)?.label)
    }

    func testThePublicListNamesTheActiveAndTheFavorite() throws {
        let (store, _) = fresh()
        let a = store.add(url: "https://a.example.com", label: "A")
        let b = store.add(url: "https://b.example.com", label: "B")
        store.setActive(b.id)
        let rows = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(store.publicJSON().utf8)) as? [[String: Any]])
        XCTAssertEqual(2, rows.count)
        XCTAssertEqual(a.id, rows[0]["id"] as? String)
        XCTAssertEqual(true, rows[0]["favorite"] as? Bool)
        XCTAssertEqual(false, rows[0]["active"] as? Bool)
        XCTAssertEqual(true, rows[1]["active"] as? Bool)
        XCTAssertNil(rows[0]["installId"], "the proxy's id stays inside the app")
    }

    func testABrokenJsonStartsEmpty() {
        let backing = MemoryBacking()
        backing.set("not json", forKey: InstallStore.key)
        let store = InstallStore(backing: backing)
        XCTAssertTrue(store.list().isEmpty)
        XCTAssertNil(store.active())
    }

    func testHostOfReadsAnAddressTolerantly() {
        XCTAssertEqual("a.example.com", InstallStore.hostOf("https://A.example.com/path"))
        XCTAssertEqual("192.168.1.10", InstallStore.hostOf("http://192.168.1.10:8400"))
        XCTAssertEqual("[2001:db8::1]", InstallStore.hostOf("https://[2001:db8::1]:8443"))
        XCTAssertEqual("plain", InstallStore.hostOf("plain/x"))
        XCTAssertNil(InstallStore.hostOf(nil))
    }
}

/// The sign-in flow's arming, expiry and the deep link's target.
final class AuthFlowTests: XCTestCase {

    func testAnArmedFlowTakesItsOwnKindForItsInstallUntilTheDeadline() {
        var now: Int64 = 1_000_000
        let backing = MemoryBacking()
        let flow = AuthFlow(backing: backing, now: { now })
        flow.arm(installId: "i1", kind: "auth")
        XCTAssertEqual("i1", flow.target(linkKind: "auth", taggedId: nil))
        XCTAssertNil(flow.target(linkKind: "oauth", taggedId: nil))
        XCTAssertNil(flow.target(linkKind: "auth", taggedId: "i2"))
        XCTAssertEqual("i1", flow.target(linkKind: "auth", taggedId: "i1"))
        now += NativeBridgePolicy.authFlowTTLMs + 1
        XCTAssertNil(flow.target(linkKind: "auth", taggedId: nil), "expired")
    }

    func testAClearedFlowTakesNothing() {
        let flow = AuthFlow(backing: MemoryBacking(), now: { 10 })
        flow.arm(installId: "i1", kind: "oauth")
        flow.clear()
        XCTAssertNil(flow.target(linkKind: "oauth", taggedId: nil))
    }

    func testARequestWhileTheBrowserIsUpIsRefusedAndTheFirstFlowStays() {
        let flow = AuthFlow(backing: MemoryBacking(), now: { 10 })
        XCTAssertTrue(flow.arm(installId: "i1", kind: "auth", unlessOpen: false))
        XCTAssertFalse(flow.arm(installId: "i2", kind: "oauth", unlessOpen: true), "the sheet is up: refused")
        XCTAssertEqual("i1", flow.target(linkKind: "auth", taggedId: nil), "the first sign-in's result still lands")
        XCTAssertNil(flow.target(linkKind: "oauth", taggedId: nil), "nothing of the refused request was armed")
        XCTAssertFalse(flow.arm(installId: "i1", kind: "auth", unlessOpen: true), "a repeat of the same request is refused too")
        XCTAssertEqual("i1", flow.target(linkKind: "auth", taggedId: nil))
    }

    func testADeadlineFromBeforeARebootReadsAsExpired() {
        let backing = MemoryBacking()
        let armed = AuthFlow(backing: backing, now: { 50_000_000 })
        armed.arm(installId: "i1", kind: "auth")
        let rebooted = AuthFlow(backing: backing, now: { 1_000 })
        XCTAssertNil(rebooted.target(linkKind: "auth", taggedId: nil))
    }

    func testTheLinkKindIsTheHost() {
        XCTAssertEqual("auth", AuthFlow.linkKind(ofHost: "auth"))
        XCTAssertEqual("oauth", AuthFlow.linkKind(ofHost: "oauth"))
        XCTAssertNil(AuthFlow.linkKind(ofHost: "other"))
        XCTAssertNil(AuthFlow.linkKind(ofHost: nil))
    }

    func testTheMonotonicClockCounts() {
        let a = MonotonicClock.nowMs()
        let b = MonotonicClock.nowMs()
        XCTAssertGreaterThan(a, 0)
        XCTAssertGreaterThanOrEqual(b, a)
    }
}

/// A route is a plain absolute path; anything else reads as the root.
final class RoutesTests: XCTestCase {

    func testOnlyAPlainAbsolutePathIsARoute() {
        XCTAssertTrue(Routes.isSafeRoutePath("/chat/pa/1?x=1"))
        XCTAssertFalse(Routes.isSafeRoutePath("//evil.example"))
        XCTAssertFalse(Routes.isSafeRoutePath("https://evil.example/"))
        XCTAssertFalse(Routes.isSafeRoutePath("/a\\b"))
        XCTAssertFalse(Routes.isSafeRoutePath("/a b"))
        XCTAssertFalse(Routes.isSafeRoutePath("/a\u{2028}b"))
        XCTAssertFalse(Routes.isSafeRoutePath(""))
        XCTAssertFalse(Routes.isSafeRoutePath(nil))
        XCTAssertFalse(Routes.isSafeRoutePath("chat"))
    }

    func testAClickUrlReducesToARouteOrTheRoot() {
        XCTAssertEqual("/apps/x", Routes.sanitizeClickUrl(" /apps/x "))
        XCTAssertEqual("/", Routes.sanitizeClickUrl("javascript:alert(1)"))
        XCTAssertEqual("/", Routes.sanitizeClickUrl(nil))
    }
}

/// The forward-auth sibling rule.
final class AuthSiblingTests: XCTestCase {

    func testASiblingOfTheActiveHostsParentStaysInTheWebView() {
        XCTAssertTrue(AuthSibling.isSibling(scheme: "https", requestHost: "auth.example.com", activeHost: "agents.example.com"))
        XCTAssertTrue(AuthSibling.isSibling(scheme: "HTTPS", requestHost: "Auth.Example.com", activeHost: "agents.example.com"))
        XCTAssertTrue(AuthSibling.isSibling(scheme: "https", requestHost: "auth.company.co.uk", activeHost: "app.company.co.uk"))
    }

    func testNoSiblingForABareDomainAnIpAPublicSuffixOrPlainHttp() {
        XCTAssertFalse(AuthSibling.isSibling(scheme: "https", requestHost: "agents.example.com", activeHost: "agents.example.com"), "the same host")
        XCTAssertFalse(AuthSibling.isSibling(scheme: "https", requestHost: "auth.example.com", activeHost: "example.com"), "a bare domain")
        XCTAssertFalse(AuthSibling.isSibling(scheme: "https", requestHost: "other.co.uk", activeHost: "myserver.co.uk"), "a public suffix")
        XCTAssertFalse(AuthSibling.isSibling(scheme: "https", requestHost: "10.0.0.6", activeHost: "10.0.0.5"), "an IP literal")
        XCTAssertFalse(AuthSibling.isSibling(scheme: "http", requestHost: "auth.example.com", activeHost: "agents.example.com"), "plain http")
        XCTAssertFalse(AuthSibling.isSibling(scheme: "https", requestHost: "evil.example.org", activeHost: "agents.example.com"), "another parent")
        XCTAssertFalse(AuthSibling.isSibling(scheme: "https", requestHost: nil, activeHost: "agents.example.com"))
        XCTAssertFalse(AuthSibling.isSibling(scheme: "https", requestHost: "auth.example.com", activeHost: nil))
    }
}
