import Foundation

/// A clock that counts while the device sleeps and restarts at a reboot,
/// the behaviour Android's `elapsedRealtime` gives the sign-in deadline.
/// Not the boot-time API Apple asks a reason for (`systemUptime`).
public enum MonotonicClock {
    public static func nowMs() -> Int64 {
        return Int64(clock_gettime_nsec_np(CLOCK_MONOTONIC) / 1_000_000)
    }
}

/// The sign-in the app opened in the system browser: the deep link that ends
/// it is taken only while it is in flight, only for its kind (auth or oauth)
/// and only for the install that started it. Persisted, so it survives the
/// process being killed while the browser is in front.
public final class AuthFlow {

    static let installKey = "otodock_prefs.auth_initiating_install"
    static let kindKey = "otodock_prefs.auth_flow_kind"
    static let untilKey = "otodock_prefs.auth_flow_until"

    private let backing: KeyValueBacking
    private let now: () -> Int64

    public init(backing: KeyValueBacking, now: @escaping () -> Int64 = MonotonicClock.nowMs) {
        self.backing = backing
        self.now = now
    }

    /// Arm the sign-in the system browser is about to run.
    public func arm(installId: String, kind: String) {
        backing.set(installId, forKey: Self.installKey)
        backing.set(kind, forKey: Self.kindKey)
        backing.set(String(now() + NativeBridgePolicy.authFlowTTLMs), forKey: Self.untilKey)
    }

    /// Arm for a request, unless the browser already shows a sign-in: that
    /// request is refused and the running sign-in keeps its flow, so its
    /// result still lands. True when armed.
    public func arm(installId: String, kind: String, unlessOpen open: Bool) -> Bool {
        if open { return false }
        arm(installId: installId, kind: kind)
        return true
    }

    public func clear() {
        backing.set(nil, forKey: Self.installKey)
        backing.set(nil, forKey: Self.kindKey)
        backing.set(nil, forKey: Self.untilKey)
    }

    /// The local id of the install a link of `linkKind` goes to, or nil to
    /// drop it; `taggedId` is the local id the link's `install` tag named,
    /// when it named a known install.
    public func target(linkKind: String, taggedId: String?) -> String? {
        let until = Int64(backing.string(forKey: Self.untilKey) ?? "") ?? 0
        return NativeBridgePolicy.deepLinkTarget(
            linkKind, backing.string(forKey: Self.kindKey), until, now(),
            backing.string(forKey: Self.installKey), taggedId)
    }

    /// The link kind of a deep link's host: `auth` for `otodock://auth/...`,
    /// `oauth` for `otodock://oauth/...`, nil for anything else.
    public static func linkKind(ofHost host: String?) -> String? {
        return NativeBridgePolicy.isFlowKind(host) ? host : nil
    }
}

/// A route is a plain absolute path (one leading slash, no scheme or host,
/// no control or line-separator character that could break out of an
/// address or a script string); anything else reads as the root.
public enum Routes {
    public static func isSafeRoutePath(_ p: String?) -> Bool {
        guard let p, !p.isEmpty, p.hasPrefix("/"), !p.hasPrefix("//") else { return false }
        if p.contains("://") || p.contains("\\") { return false }
        for scalar in p.unicodeScalars {
            if scalar.value <= 0x20 || scalar.value == 0x7f || scalar.value == 0x2028 || scalar.value == 0x2029 { return false }
        }
        return true
    }

    public static func sanitizeClickUrl(_ raw: String?) -> String {
        guard let raw else { return "/" }
        let p = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return isSafeRoutePath(p) ? p : "/"
    }
}

/// The forward-auth rule: a main-frame https navigation to a sibling of the
/// active host's parent domain (active `app.example.com`, request
/// `auth.example.com`) stays in the web view, so the sign-in's cookie lands
/// in the web view's jar and it can bounce back to the active host. Its scope
/// is what a `*.<parent>` wildcard would give, minus the bridges. Bare
/// registrable domains, IP literals and LAN hosts have no siblings (a real
/// subdomain is needed: at least two dots and an alphabetic last label).
public enum AuthSibling {

    // Common multi-label public suffixes. Without the full Public Suffix List
    // this denylist keeps a host sitting directly under one (myserver.co.uk)
    // from treating every *.co.uk host as a sibling. Deeper structures
    // (app.company.co.uk) still work: their parent company.co.uk is not a
    // public suffix.
    static let multiLabelPublicSuffixes: Set<String> = [
        "co.uk", "org.uk", "gov.uk", "ac.uk", "me.uk", "net.uk", "ltd.uk", "plc.uk",
        "com.au", "net.au", "org.au", "edu.au", "gov.au", "id.au",
        "co.nz", "net.nz", "org.nz", "govt.nz", "ac.nz",
        "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp",
        "co.za", "org.za", "co.kr", "or.kr",
        "co.in", "net.in", "org.in", "gen.in", "firm.in", "ind.in",
        "com.br", "net.br", "org.br", "gov.br",
        "com.cn", "net.cn", "org.cn", "gov.cn",
        "com.mx", "com.tr", "com.tw", "com.sg", "com.hk", "com.ua", "net.ua", "org.ua",
        "co.il", "com.ar", "co.id", "or.id",
        "com.gr", "net.gr", "org.gr", "edu.gr", "gov.gr",
        "com.es", "org.es", "nom.es", "gob.es", "edu.es",
        "com.pl", "net.pl", "org.pl", "com.ru", "net.ru", "org.ru",
        "co.at", "or.at",
    ]

    public static func isSibling(scheme: String?, requestHost: String?, activeHost: String?) -> Bool {
        guard scheme?.lowercased() == "https", let requestHost, let activeHost else { return false }
        if requestHost.lowercased() == activeHost.lowercased() { return false }
        guard let firstDot = activeHost.firstIndex(of: "."), firstDot != activeHost.startIndex else { return false }
        let afterFirst = activeHost.index(after: firstDot)
        guard activeHost[afterFirst...].contains(".") else { return false }
        guard let last = activeHost.last, !last.isNumber else { return false }
        let parentSuffix = activeHost[firstDot...].lowercased()  // ".example.com"
        if multiLabelPublicSuffixes.contains(String(parentSuffix.dropFirst())) { return false }
        return requestHost.lowercased().hasSuffix(parentSuffix)
    }
}
