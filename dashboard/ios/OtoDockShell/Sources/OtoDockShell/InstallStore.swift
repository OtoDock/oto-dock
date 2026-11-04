import Foundation

/// Where the shell keeps its small state: `UserDefaults` in the app, a
/// dictionary in the tests.
public protocol KeyValueBacking: AnyObject {
    func string(forKey key: String) -> String?
    func set(_ value: String?, forKey key: String)
}

public final class UserDefaultsBacking: KeyValueBacking {
    private let defaults: UserDefaults

    public init(_ defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    public func string(forKey key: String) -> String? {
        return defaults.string(forKey: key)
    }

    public func set(_ value: String?, forKey key: String) {
        if let value { defaults.set(value, forKey: key) } else { defaults.removeObject(forKey: key) }
    }
}

public final class MemoryBacking: KeyValueBacking {
    public private(set) var values: [String: String] = [:]

    public init() {}

    public func string(forKey key: String) -> String? {
        return values[key]
    }

    public func set(_ value: String?, forKey key: String) {
        values[key] = value
    }
}

/// Persistent registry of connected OtoDock server installations: a list of
/// installations plus an active pointer, so the app can hold and switch
/// between several servers. One install is the favorite (the cold-start
/// target). The same JSON the Android app keeps under
/// `otodock_prefs/installations`.
///
/// Invariants (enforced on every load and mutation): exactly one favorite
/// when the list is non-empty; `activeId` names an existing install (or is
/// nil when the list is empty); no two installs share a host (same-host
/// installs would collide on cookies); no two installs share a proxy
/// install id.
public final class InstallStore {

    public static let key = "otodock_prefs.installations"
    private static let version = 1

    public struct Install: Equatable {
        public var id: String
        public var url: String
        public var label: String
        public var favorite: Bool
        public var installId: String?   // the proxy's install_id (the relay identity); nil until fetched
        public var lastRoute: String?   // the app-shell path to restore on a switch-in; nil = the root
        public var addedAt: Int64
    }

    private let backing: KeyValueBacking
    private let lock = NSLock()
    private var activeId: String?
    private var installs: [Install] = []

    public init(backing: KeyValueBacking) {
        self.backing = backing
        load()
    }

    /// Lowercase host name of an address, with a tolerant fallback parse.
    public static func hostOf(_ url: String?) -> String? {
        guard let url else { return nil }
        if let origin = NativeBridgePolicy.origin(url), let host = NativeBridgePolicy.host(origin) { return host }
        var s = url.replacingOccurrences(of: "https://", with: "").replacingOccurrences(of: "http://", with: "")
        if let slash = s.firstIndex(of: "/") { s = String(s[..<slash]) }
        return s.lowercased()
    }

    // MARK: - persistence

    private func load() {
        lock.lock(); defer { lock.unlock() }
        installs = []
        activeId = nil
        guard let raw = backing.string(forKey: Self.key), let data = raw.data(using: .utf8),
              let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }
        activeId = root["activeId"] as? String
        for item in root["installs"] as? [[String: Any]] ?? [] {
            guard let id = item["id"] as? String, let url = item["url"] as? String else { continue }
            installs.append(Install(
                id: id,
                url: url,
                label: item["label"] as? String ?? Self.hostOf(url) ?? url,
                favorite: item["favorite"] as? Bool ?? false,
                installId: item["installId"] as? String,
                lastRoute: item["lastRoute"] as? String,
                addedAt: (item["addedAt"] as? NSNumber)?.int64Value ?? 0))
        }
        enforceInvariants()
    }

    private func save() {
        var root: [String: Any] = ["version": Self.version]
        if let activeId { root["activeId"] = activeId }
        root["installs"] = installs.map { install -> [String: Any] in
            var o: [String: Any] = [
                "id": install.id, "url": install.url, "label": install.label,
                "favorite": install.favorite, "addedAt": install.addedAt,
            ]
            if let installId = install.installId { o["installId"] = installId }
            if let lastRoute = install.lastRoute { o["lastRoute"] = lastRoute }
            return o
        }
        if let data = try? JSONSerialization.data(withJSONObject: root), let text = String(data: data, encoding: .utf8) {
            backing.set(text, forKey: Self.key)
        }
    }

    // MARK: - invariants

    private func enforceInvariants() {
        if installs.isEmpty {
            activeId = nil
            return
        }
        if byIdInternal(activeId) == nil {
            activeId = (installs.first { $0.favorite } ?? installs[0]).id
        }
        let favorites = installs.filter { $0.favorite }.count
        if favorites == 0 {
            installs[0].favorite = true
        } else if favorites > 1 {
            var kept = false
            for n in installs.indices where installs[n].favorite {
                if kept { installs[n].favorite = false } else { kept = true }
            }
        }
    }

    private func byIdInternal(_ id: String?) -> Install? {
        guard let id else { return nil }
        return installs.first { $0.id == id }
    }

    private func index(of id: String) -> Int? {
        return installs.firstIndex { $0.id == id }
    }

    // MARK: - reading

    public func list() -> [Install] { lock.lock(); defer { lock.unlock() }; return installs }
    public var activeIdValue: String? { lock.lock(); defer { lock.unlock() }; return activeId }
    public func active() -> Install? { lock.lock(); defer { lock.unlock() }; return byIdInternal(activeId) }
    public func favorite() -> Install? { lock.lock(); defer { lock.unlock() }; return installs.first { $0.favorite } }
    public func byId(_ id: String?) -> Install? { lock.lock(); defer { lock.unlock() }; return byIdInternal(id) }

    public func byHost(_ host: String?) -> Install? {
        guard let host = host?.lowercased() else { return nil }
        lock.lock(); defer { lock.unlock() }
        return installs.first { Self.hostOf($0.url) == host }
    }

    public func byInstallId(_ pid: String?) -> Install? {
        guard let pid else { return nil }
        lock.lock(); defer { lock.unlock() }
        return installs.first { $0.installId == pid }
    }

    // MARK: - writing

    /// Add a new install, or return the existing one on the same host.
    @discardableResult
    public func add(url: String, label: String?) -> Install {
        lock.lock(); defer { lock.unlock() }
        let host = Self.hostOf(url)
        if let existing = installs.first(where: { Self.hostOf($0.url) == host }) { return existing }
        let install = Install(
            id: UUID().uuidString.lowercased(),
            url: url,
            label: (label?.isEmpty == false ? label : nil) ?? host ?? url,
            favorite: installs.isEmpty,
            installId: nil,
            lastRoute: nil,
            addedAt: Int64(Date().timeIntervalSince1970 * 1000))
        installs.append(install)
        if activeId == nil { activeId = install.id }
        enforceInvariants()
        save()
        return install
    }

    public func remove(_ id: String) {
        lock.lock(); defer { lock.unlock() }
        guard let n = index(of: id) else { return }
        installs.remove(at: n)
        if id == activeId { activeId = nil }  // reassigned by enforceInvariants
        enforceInvariants()
        save()
    }

    public func setActive(_ id: String) {
        lock.lock(); defer { lock.unlock() }
        guard byIdInternal(id) != nil else { return }
        activeId = id
        save()
    }

    public func setFavorite(_ id: String) {
        lock.lock(); defer { lock.unlock() }
        guard byIdInternal(id) != nil else { return }
        for n in installs.indices { installs[n].favorite = installs[n].id == id }
        save()
    }

    public func rename(_ id: String, _ label: String?) {
        lock.lock(); defer { lock.unlock() }
        guard let n = index(of: id), let capped = NativeBridgePolicy.capLabel(label) else { return }
        installs[n].label = capped
        save()
    }

    /// Bind the proxy install_id to an install. First writer wins on a collision.
    @discardableResult
    public func setInstallId(_ id: String, _ pid: String?) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard let n = index(of: id), let pid else { return false }
        if let owner = installs.first(where: { $0.installId == pid }), owner.id != id { return false }
        installs[n].installId = pid
        save()
        return true
    }

    public func setLastRoute(_ id: String, _ path: String?) {
        lock.lock(); defer { lock.unlock() }
        guard let n = index(of: id) else { return }
        installs[n].lastRoute = path
        save()
    }

    /// The list for the dashboard's App Settings and the setup page's picker:
    /// id, url, label, favorite, active.
    public func publicJSON() -> String {
        lock.lock(); defer { lock.unlock() }
        let rows = installs.map { install -> [String: Any] in
            ["id": install.id, "url": install.url, "label": install.label,
             "favorite": install.favorite, "active": install.id == activeId]
        }
        guard let data = try? JSONSerialization.data(withJSONObject: rows), let text = String(data: data, encoding: .utf8) else { return "[]" }
        return text
    }
}
