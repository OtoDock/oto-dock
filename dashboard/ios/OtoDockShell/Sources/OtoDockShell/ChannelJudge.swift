import Foundation

/// What the shell decides about a message, a plugin call or a prompt from
/// the sender WebKit reports, with no WebKit in it, so the decisions run
/// under `swift test` on a Mac: the twin of the checks the Android
/// MainActivity makes in its message listener. The app target turns the
/// WebKit objects into a `Sender` and acts on the verdict.
public enum ChannelJudge {

    /// The asset handler's origin, where the bundled setup page lives.
    public static let localOrigin = "capacitor://localhost"
    public static let setupPagePath = "/setup.html"

    /// Capacitor's core plugins that read other hosts' cookies or persist a
    /// base path the next launch cannot load: refused by name on every page.
    public static let refusedPlugins: Set<String> = ["CapacitorHttp", "CapacitorCookies", "WebView"]

    /// A message's sender, as WebKit reports it: whether the posting frame is
    /// the main frame, its security origin and the main frame's URL.
    public struct Sender: Equatable {
        public let isMainFrame: Bool
        public let origin: String
        public let pageURL: String?

        public init(isMainFrame: Bool, origin: String, pageURL: String?) {
            self.isMainFrame = isMainFrame
            self.origin = origin
            self.pageURL = pageURL
        }
    }

    /// What becomes of a message on the dashboard channel.
    public enum Verdict: Equatable {
        /// The active install's dashboard: dispatched as its call.
        case dashboard
        /// The bundled setup page's own method.
        case setup
        /// Dropped, with the reason the log line names.
        case refused(String)
    }

    /// What becomes of a message on Capacitor's own handler.
    public enum BridgeVerdict: Equatable {
        case forwarded
        case refused(String)
    }

    // MARK: - origins and pages

    /// `scheme://host[:port]` as the policy writes an origin, from the parts
    /// WebKit reports (an IPv6 host bracketed when WebKit left the brackets
    /// off; a port of 0 is no port).
    public static func originString(protocol scheme: String, host: String, port: Int) -> String {
        var host = host
        if host.contains(":") && !host.hasPrefix("[") { host = "[" + host + "]" }
        let port = port == 0 ? "" : ":\(port)"
        return "\(scheme)://\(host)\(port)"
    }

    /// True when the address is the bundled setup page at the asset
    /// handler's origin: a server cannot serve its own `/setup.html` to
    /// reach the setup-only methods.
    public static func isSetupPage(_ url: String?) -> Bool {
        guard let parts = NativeBridgePolicy.parse(url), parts.scheme.lowercased() == "capacitor",
              parts.authority?.lowercased() == "localhost" else { return false }
        return pathOnly(parts.path) == setupPagePath
    }

    /// True for the setup page shown over a server that did not answer
    /// (`error=unreachable` in its query): the one screen with the picker
    /// of the other servers. The add screen and the first run show none.
    public static func isUnreachablePage(_ url: String?) -> Bool {
        guard isSetupPage(url), let parts = NativeBridgePolicy.parse(url) else { return false }
        return query(of: parts.path).split(separator: "&").contains("error=unreachable")
    }

    private static func pathOnly(_ path: String) -> String {
        let end = path.firstIndex(where: { $0 == "?" || $0 == "#" }) ?? path.endIndex
        return String(path[..<end])
    }

    private static func query(of path: String) -> Substring {
        guard let mark = path.firstIndex(of: "?") else { return "" }
        let rest = path[path.index(after: mark)...]
        let end = rest.firstIndex(of: "#") ?? rest.endIndex
        return rest[..<end]
    }

    // MARK: - the dashboard channel

    /// Judge a message on `OtoDockNative`, in Android's order: from the main
    /// frame; from the bundled setup page or the active origin; a text within
    /// the size cap (UTF-16 units, Android's `length()`); then the page: the
    /// setup page's own methods and nothing else, or a dashboard page not
    /// under `/v1/` with the dashboard's call, a `setup.*` method excepted.
    public static func judgeChannel(_ sender: Sender, body: Any?, activeOrigin: String?) -> Verdict {
        guard sender.isMainFrame else { return .refused("a child frame") }
        let local = sender.origin == localOrigin
        if !local {
            guard let activeOrigin, NativeBridgePolicy.sameOrigin(activeOrigin, sender.origin) else { return .refused("another origin") }
        }
        guard let text = body as? String else { return .refused("a non-text message") }
        guard text.utf16.count <= NativeBridgePolicy.maxMessageChars else { return .refused("an oversized message") }
        if local {
            return isSetupPage(sender.pageURL) ? .setup : .refused("a local page that is not the setup page")
        }
        guard NativeBridgePolicy.isDashboardPage(sender.pageURL) else { return .refused("a page outside the dashboard") }
        if text.hasPrefix("{\"m\":\"setup.") { return .refused("a setup method from the dashboard") }
        return .dashboard
    }

    // MARK: - Capacitor's handler

    /// Judge a message on Capacitor's `bridge` handler: the main frame only;
    /// never one of the refused plugins; from the bundled setup page the
    /// console alone; otherwise from the active origin.
    public static func judgeBridge(_ sender: Sender, pluginId: String, activeOrigin: String?) -> BridgeVerdict {
        guard sender.isMainFrame else { return .refused("a child frame") }
        if refusedPlugins.contains(pluginId) { return .refused("the \(pluginId) plugin") }
        if sender.origin == localOrigin {
            return pluginId == "Console" && isSetupPage(sender.pageURL) ? .forwarded : .refused("the setup page")
        }
        guard let activeOrigin, NativeBridgePolicy.sameOrigin(activeOrigin, sender.origin) else { return .refused("another origin") }
        return .forwarded
    }

    /// Capacitor's cookie and HTTP plugins ask the native side through
    /// `prompt()` payloads (`CapacitorCookies.get`, `.set`, `.isEnabled`,
    /// `CapacitorHttp`), from any frame: answered empty or false, never
    /// forwarded, so no page writes a cookie for another host. Nil for any
    /// other prompt, which goes to the page's own dialog.
    public static func promptAnswer(_ prompt: String) -> String? {
        guard let data = prompt.data(using: .utf8),
              let payload = (try? JSONSerialization.jsonObject(with: data, options: .fragmentsAllowed)) as? [String: Any],
              let type = payload["type"] as? String else { return nil }
        switch type {
        case "CapacitorCookies.get", "CapacitorCookies.set": return ""
        case "CapacitorCookies.isEnabled", "CapacitorHttp": return "false"
        default: return nil
        }
    }
}
