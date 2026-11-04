import Foundation

/// The native bridge's rules, kept free of UIKit and WebKit so plain XCTest
/// runs them on a Mac: which origin the dashboard channel serves, what a
/// saved file may be, which address the system browser may open, and when
/// a deep link is taken. The twin of the Android app's `NativeBridgePolicy`.
public enum NativeBridgePolicy {

    /// Name of the origin-scoped object the dashboard posts on.
    public static let channel = "OtoDockNative"

    public static let maxSaveBytes = 25_000_000
    // The base64 of maxSaveBytes plus the JSON header, refused before any parsing.
    public static let maxMessageChars = 36 * 1024 * 1024
    public static let maxLabelCodePoints = 80
    static let maxSaveNameChars = 100

    /// OtoDock's privacy policy for the apps: the stores require a link to it
    /// inside the app. Fixed here, so no page can name another address.
    public static let privacyPolicyURL = "https://otodock.io/legal/privacy"

    /// How long a sign-in the app opened may take to come back through its deep link.
    public static let authFlowTTLMs: Int64 = 10 * 60 * 1000
    public static let flowAuth = "auth"
    public static let flowOAuth = "oauth"

    // The lightbox's image types (ImageLightbox.tsx MIME_EXT) and the diagnostics.
    private static let saveTypes: [String: String] = [
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
        "image/svg+xml": "svg",
        "image/heic": "heic",
        "image/heif": "heif",
        "image/avif": "avif",
        "image/bmp": "bmp",
        "image/tiff": "tiff",
        "application/json": "json",
    ]

    // MARK: - origins

    /// `scheme://host[:port]` of an http(s) address with a host and no user
    /// info, the host in lower-case ASCII (an IPv6 literal bracketed in the
    /// form WebKit reports origins in) and the port only when it is not the
    /// scheme's default; nil for anything else. The path is ignored.
    public static func origin(_ url: String?) -> String? {
        guard let parts = parse(url) else { return nil }
        let scheme = parts.scheme.lowercased()
        guard scheme == "https" || scheme == "http" else { return nil }
        guard let authority = parts.authority, !authority.contains("@") else { return nil }
        guard let host = asciiHost(authority) else { return nil }
        guard let port = port(of: authority) else { return nil }
        let defaultPort = port == -1
            || (scheme == "https" && port == 443)
            || (scheme == "http" && port == 80)
        return "\(scheme)://\(host)" + (defaultPort ? "" : ":\(port)")
    }

    public static func sameOrigin(_ a: String?, _ b: String?) -> Bool {
        guard let oa = origin(a) else { return false }
        return oa == origin(b)
    }

    /// Host part of an origin produced by `origin`.
    public static func host(_ origin: String?) -> String? {
        guard let parts = parse(origin), let authority = parts.authority else { return nil }
        return asciiHost(authority)
    }

    /// False for the API prefix: no dashboard page lives there, only API
    /// answers and served content.
    public static func isDashboardPage(_ topURL: String?) -> Bool {
        guard let parts = parse(topURL) else { return false }
        return !parts.path.hasPrefix("/v1/")
    }

    /// The parts of an address: the scheme, the authority when the address
    /// has one, the path (with the query and fragment left on it). Nil when
    /// the text is not an address at all: a backslash, white space or a
    /// control character inside, or no scheme.
    struct Parts {
        let scheme: String
        let authority: String?
        let path: String
    }

    static func parse(_ url: String?) -> Parts? {
        guard let raw = url else { return nil }
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.isEmpty || text.contains("\\") { return nil }
        for scalar in text.unicodeScalars {
            if scalar.value <= 0x20 || scalar.value == 0x7f { return nil }
        }
        guard let colon = text.firstIndex(of: ":") else { return nil }
        let scheme = String(text[..<colon])
        guard isScheme(scheme) else { return nil }
        let rest = text[text.index(after: colon)...]
        guard rest.hasPrefix("//") else { return Parts(scheme: scheme, authority: nil, path: String(rest)) }
        let afterSlashes = rest.dropFirst(2)
        let end = afterSlashes.firstIndex(where: { $0 == "/" || $0 == "?" || $0 == "#" }) ?? afterSlashes.endIndex
        let authority = String(afterSlashes[..<end])
        let path = String(afterSlashes[end...])
        return Parts(scheme: scheme, authority: authority, path: path)
    }

    private static func isScheme(_ s: String) -> Bool {
        guard let first = s.unicodeScalars.first, CharacterSet.letters.contains(first), first.isASCII else { return false }
        for scalar in s.unicodeScalars {
            guard scalar.isASCII else { return false }
            let c = Character(scalar)
            if !(c.isLetter || c.isNumber || c == "+" || c == "." || c == "-") { return false }
        }
        return true
    }

    private static let badPort = -2

    /// The port, -1 for none, nil for one no address can carry (past 65535
    /// or more than five digits).
    static func port(of authority: String) -> Int? {
        let tail: Substring
        if authority.hasPrefix("[") {
            guard let close = authority.firstIndex(of: "]") else { return -1 }
            tail = authority[authority.index(after: close)...]
            if tail.isEmpty { return -1 }
            guard tail.hasPrefix(":") else { return -1 }
        } else {
            guard let colon = authority.lastIndex(of: ":") else { return -1 }
            tail = authority[colon...]
        }
        let digits = tail.dropFirst()
        if digits.isEmpty { return -1 }
        guard digits.allSatisfy({ $0.isASCII && $0.isNumber }) else { return -1 }
        if digits.count > 5 { return nil }
        guard let value = Int(digits), value <= 65535 else { return nil }
        return value
    }

    /// The host of an authority in lower-case ASCII, or nil when it is not a
    /// host name of letters, digits, dots, hyphens and underscores, nor a
    /// well-formed bracketed IPv6 literal.
    static func asciiHost(_ authority: String) -> String? {
        if authority.contains("@") { return nil }
        if authority.hasPrefix("[") {
            guard let close = authority.firstIndex(of: "]") else { return nil }
            let rest = authority[authority.index(after: close)...]
            if !rest.isEmpty && !(rest.hasPrefix(":") && rest.dropFirst().allSatisfy { $0.isASCII && $0.isNumber }) { return nil }
            return ipv6Literal(String(authority[...close]))
        }
        var host = authority
        if let colon = host.lastIndex(of: ":"), host[host.index(after: colon)...].allSatisfy({ $0.isASCII && $0.isNumber }) {
            host = String(host[..<colon])
        }
        if host.isEmpty { return nil }
        guard let ascii = IDNA.toASCII(host) else { return nil }
        for scalar in ascii.unicodeScalars {
            let c = Character(scalar)
            let ok = scalar.isASCII && (c.isLetter || c.isNumber || c == "." || c == "-" || c == "_")
            if !ok { return nil }
        }
        return ascii
    }

    /// A bracketed IPv6 literal in the URL Standard's serialisation, the form
    /// WebKit reports an origin in (lower-case hex without leading zeros, the
    /// first longest run of two or more zero pieces as "::", an IPv4 tail as
    /// two hex pieces); nil for anything else, a zone id included. Parsed by
    /// hand, as Android does.
    public static func ipv6Literal(_ bracketed: String) -> String? {
        guard bracketed.count >= 3, bracketed.hasPrefix("["), bracketed.hasSuffix("]") else { return nil }
        let inner = String(bracketed.dropFirst().dropLast()).lowercased()
        guard let pieces = ipv6Pieces(inner) else { return nil }
        var runStart = -1
        var runLength = 0
        var i = 0
        while i < 8 {
            if pieces[i] != 0 { i += 1; continue }
            var j = i
            while j < 8 && pieces[j] == 0 { j += 1 }
            if j - i > runLength { runStart = i; runLength = j - i }
            i = j
        }
        if runLength < 2 { runStart = -1 }
        var out = "["
        var k = 0
        while k < 8 {
            if k == runStart {
                out += "::"
                k += runLength
                continue
            }
            if out.count > 1 && !out.hasSuffix(":") { out += ":" }
            out += String(pieces[k], radix: 16)
            k += 1
        }
        return out + "]"
    }

    /// The eight 16-bit pieces of an IPv6 address, or nil when malformed.
    static func ipv6Pieces(_ address: String) -> [Int]? {
        let gap = address.range(of: "::")
        if let gap, address[gap.upperBound...].contains("::") { return nil }
        let head = gap.map { String(address[..<$0.lowerBound]) } ?? address
        let tail = gap.map { String(address[$0.upperBound...]) } ?? ""
        // A dotted IPv4 tail is allowed only at the very end of the address.
        guard let h = hexPieces(head, ipv4TailAllowed: gap == nil), let t = hexPieces(tail, ipv4TailAllowed: true) else { return nil }
        if gap == nil ? h.count != 8 : h.count + t.count > 7 { return nil }
        var pieces = [Int](repeating: 0, count: 8)
        for (n, value) in h.enumerated() { pieces[n] = value }
        for (n, value) in t.enumerated() { pieces[8 - t.count + n] = value }
        return pieces
    }

    private static func hexPieces(_ part: String, ipv4TailAllowed: Bool) -> [Int]? {
        if part.isEmpty { return [] }
        let groups = part.split(separator: ":", omittingEmptySubsequences: false).map(String.init)
        let last = groups[groups.count - 1]
        let ipv4 = last.contains(".")
        if ipv4 && !ipv4TailAllowed { return nil }
        var out = [Int](repeating: 0, count: groups.count + (ipv4 ? 1 : 0))
        let hexCount = groups.count - (ipv4 ? 1 : 0)
        for n in 0..<hexCount {
            let g = groups[n]
            guard (1...4).contains(g.count), g.allSatisfy({ $0.isHexDigit && ($0.isNumber || $0.isLowercase) }), let v = Int(g, radix: 16) else { return nil }
            out[n] = v
        }
        if ipv4 {
            let octets = last.split(separator: ".", omittingEmptySubsequences: false).map(String.init)
            if octets.count != 4 { return nil }
            var b = [Int](repeating: 0, count: 4)
            for n in 0..<4 {
                guard isIPv4Octet(octets[n]), let v = Int(octets[n]), v <= 255 else { return nil }
                b[n] = v
            }
            out[out.count - 2] = b[0] << 8 | b[1]
            out[out.count - 1] = b[2] << 8 | b[3]
        }
        return out
    }

    /// `0`, or one to three digits with no leading zero: what the WebView
    /// reads as a decimal octet (a leading zero reads as octal).
    private static func isIPv4Octet(_ s: String) -> Bool {
        if s == "0" { return true }
        guard (1...3).contains(s.count), s.allSatisfy({ $0.isASCII && $0.isNumber }) else { return false }
        return !s.hasPrefix("0")
    }

    // MARK: - server addresses

    /// The address a typed server URL is saved as: trimmed, one trailing
    /// slash dropped, https added to a bare host. Nil for another scheme, for
    /// no origin (see `origin`) and for plain http to a host `cleartextHost`
    /// does not allow, so an address the app cannot boot is never saved and a
    /// public host never loads in cleartext.
    public static func serverUrl(_ raw: String?) -> String? {
        guard let raw else { return nil }
        var url = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if url.isEmpty { return nil }
        if url.hasSuffix("/") { url.removeLast() }
        let http = url.lowercased().hasPrefix("http://")
        if !http && !url.lowercased().hasPrefix("https://") {
            if hasSchemePrefix(url) { return nil }
            url = "https://" + url
        }
        guard let origin = origin(url) else { return nil }
        if http && !cleartextHost(host(origin)) { return nil }
        return url
    }

    private static func hasSchemePrefix(_ url: String) -> Bool {
        guard let range = url.range(of: "://") else { return false }
        return isScheme(String(url[..<range.lowerBound]))
    }

    /// Whether the app may boot an origin produced by `origin`: https, or
    /// plain http to a host `cleartextHost` allows. Saved installs are read
    /// through it too, so one saved under an older, looser rule does not load
    /// a public address in cleartext.
    public static func bootable(_ origin: String?) -> Bool {
        guard let origin else { return false }
        if origin.hasPrefix("https://") { return true }
        return origin.hasPrefix("http://") && cleartextHost(host(origin))
    }

    /// The hosts plain http may reach, as `host` gives them: an IPv4 address
    /// in 10/8, 172.16/12, 192.168/16, 127/8 or 169.254/16; an IPv6 address
    /// that is ::1 or in fc00::/7 or fe80::/10, or an IPv4-mapped one under
    /// the IPv4 rule; "localhost" and a ".local" name. An octet with a leading
    /// zero is refused: the WebView reads it as octal, so "0127.x.y.z" is not
    /// the loopback address it looks like.
    public static func cleartextHost(_ host: String?) -> Bool {
        guard let host else { return false }
        let h = host.lowercased()
        if h == "localhost" || h.hasSuffix(".local") { return true }
        if h.hasPrefix("[") {
            guard h.count > 2, h.hasSuffix("]"), let p = ipv6Pieces(String(h.dropFirst().dropLast())) else { return false }
            if zeroPieces(p, 7) && p[7] == 1 { return true }
            if (p[0] & 0xfe00) == 0xfc00 || (p[0] & 0xffc0) == 0xfe80 { return true }
            return zeroPieces(p, 5) && p[5] == 0xffff && privateIPv4(p[6] >> 8, p[6] & 0xff)
        }
        let parts = h.split(separator: ".", omittingEmptySubsequences: false).map(String.init)
        if parts.count != 4 { return false }
        var o = [Int](repeating: 0, count: 4)
        for n in 0..<4 {
            guard isIPv4Octet(parts[n]), let v = Int(parts[n]), v <= 255 else { return false }
            o[n] = v
        }
        return privateIPv4(o[0], o[1])
    }

    private static func zeroPieces(_ pieces: [Int], _ count: Int) -> Bool {
        for n in 0..<count where pieces[n] != 0 { return false }
        return true
    }

    /// By the first two octets of an IPv4 address.
    private static func privateIPv4(_ a: Int, _ b: Int) -> Bool {
        return a == 10 || (a == 172 && b >= 16 && b <= 31) || (a == 192 && b == 168)
            || a == 127 || (a == 169 && b == 254)
    }

    // MARK: - messages

    /// A message is a JSON header, optionally followed by one newline and a
    /// raw body (JSON.stringify never emits a raw newline, so the first one
    /// splits).
    public static func splitMessage(_ data: String) -> (head: String, body: String?) {
        guard let nl = data.firstIndex(of: "\n") else { return (data, nil) }
        return (String(data[..<nl]), String(data[data.index(after: nl)...]))
    }

    // MARK: - saving files

    public static func normalizeMime(_ mime: String?) -> String? {
        guard let mime else { return nil }
        var m = mime.trimmingCharacters(in: .whitespaces)
        if let semi = m.firstIndex(of: ";") { m = String(m[..<semi]).trimmingCharacters(in: .whitespaces) }
        m = m.lowercased()
        return m == "image/jpg" ? "image/jpeg" : m
    }

    /// The file extension for a type the app saves, or nil when refused.
    public static func saveExtension(_ mime: String?) -> String? {
        guard let m = normalizeMime(mime) else { return nil }
        return saveTypes[m]
    }

    /// The requested name's last path segment without its extension, reduced
    /// to plain characters, with the extension of the type.
    public static func saveFileName(_ requested: String?, _ ext: String) -> String {
        var base = requested ?? ""
        if let cut = base.lastIndex(where: { $0 == "/" || $0 == "\\" }) {
            base = String(base[base.index(after: cut)...])
        }
        while let first = base.first, first == "." || first.isWhitespace { base.removeFirst() }
        if let dot = base.lastIndex(of: "."), dot != base.startIndex { base = String(base[..<dot]) }
        base = String(base.map { c -> Character in
            (c.isASCII && (c.isLetter || c.isNumber)) || c == " " || c == "_" || c == "." || c == "-" ? c : "_"
        }).trimmingCharacters(in: .whitespaces)
        if base.count > maxSaveNameChars { base = String(base.prefix(maxSaveNameChars)) }
        if base.isEmpty { base = "image" }
        return base + "." + ext
    }

    /// Whether a base64 payload of this length can decode within the cap.
    public static func saveBase64Fits(_ chars: Int) -> Bool {
        return Int64(chars) * 3 / 4 <= Int64(maxSaveBytes) + 2
    }

    // MARK: - the switcher

    public static func capLabel(_ label: String?) -> String? {
        guard let label else { return nil }
        let l = label.trimmingCharacters(in: .whitespacesAndNewlines)
        if l.isEmpty { return nil }
        let scalars = Array(l.unicodeScalars)
        if scalars.count <= maxLabelCodePoints { return l }
        var view = String.UnicodeScalarView()
        view.append(contentsOf: scalars.prefix(maxLabelCodePoints))
        return String(view)
    }

    // MARK: - the system browser

    /// The address to hand the system browser, re-serialised from the parsed
    /// form: https only, a host, no user info; nil otherwise.
    public static func authBrowserUrl(_ url: String?) -> String? {
        guard let parts = parse(url), parts.scheme.lowercased() == "https" else { return nil }
        guard let authority = parts.authority, !authority.contains("@"), let host = asciiHost(authority) else { return nil }
        guard let port = port(of: authority) else { return nil }
        let rest = parts.path.addingPercentEncoding(withAllowedCharacters: .urlPathQueryFragment) ?? parts.path
        return "https://\(host)" + (port == -1 ? "" : ":\(port)") + rest
    }

    /// True when the address is not on the active install's origin, so the
    /// app names the other site and asks before opening it.
    public static func needsHostConfirm(_ url: String?, _ activeOrigin: String?) -> Bool {
        guard let target = origin(url) else { return true }
        return target != activeOrigin
    }

    public static func isFlowKind(_ kind: String?) -> Bool {
        return kind == flowAuth || kind == flowOAuth
    }

    // MARK: - the deep link

    /// Whether a flow of `linkKind` is armed and within its window. A
    /// deadline further ahead than the window reads as expired (the clock it
    /// was taken on restarted).
    public static func flowPending(_ armedKind: String?, _ until: Int64, _ linkKind: String, _ now: Int64) -> Bool {
        return armedKind != nil && armedKind == linkKind && until > now && until - now <= authFlowTTLMs
    }

    /// The local id of the install a deep link goes to, or nil to drop it:
    /// only while a flow of its kind is in flight, only to the install that
    /// started it (a tag naming another known install drops it).
    public static func deepLinkTarget(_ linkKind: String, _ armedKind: String?, _ until: Int64, _ now: Int64,
                                      _ initiatingId: String?, _ taggedId: String?) -> String? {
        guard flowPending(armedKind, until, linkKind, now), let initiatingId else { return nil }
        if let taggedId, taggedId != initiatingId { return nil }
        return initiatingId
    }
}

private extension CharacterSet {
    /// The characters a path, query and fragment may carry unencoded.
    static let urlPathQueryFragment = CharacterSet.urlPathAllowed.union(.urlQueryAllowed).union(.urlFragmentAllowed).union(CharacterSet(charactersIn: "?#"))
}

/// The ASCII form of a host name: every label lower-cased and compatibility
/// normalised, a non-ASCII label as its punycode (RFC 3492) with the
/// `xn--` prefix, which is how WebKit reports an internationalised host.
/// Nil when a label cannot be encoded.
enum IDNA {
    static func toASCII(_ host: String) -> String? {
        let normalised = host.precomposedStringWithCompatibilityMapping.lowercased()
        var labels: [String] = []
        for label in normalised.split(separator: ".", omittingEmptySubsequences: false) {
            if label.unicodeScalars.allSatisfy({ $0.isASCII }) {
                labels.append(String(label))
            } else {
                guard let encoded = punycode(String(label)) else { return nil }
                labels.append("xn--" + encoded)
            }
        }
        return labels.joined(separator: ".")
    }

    private static let base = 36, tMin = 1, tMax = 26, skew = 38, damp = 700, initialBias = 72, initialN = 128

    private static func adapt(_ delta: Int, _ numPoints: Int, _ firstTime: Bool) -> Int {
        var delta = firstTime ? delta / damp : delta / 2
        delta += delta / numPoints
        var k = 0
        while delta > ((base - tMin) * tMax) / 2 {
            delta /= base - tMin
            k += base
        }
        return k + (base - tMin + 1) * delta / (delta + skew)
    }

    private static func digit(_ d: Int) -> Character {
        return Character(UnicodeScalar(d < 26 ? 97 + d : 22 + d)!)
    }

    static func punycode(_ input: String) -> String? {
        let points = input.unicodeScalars.map { Int($0.value) }
        var output = ""
        for p in points where p < 0x80 { output.append(Character(UnicodeScalar(p)!)) }
        let basicCount = output.count
        var handled = basicCount
        if basicCount > 0 { output.append("-") }
        var n = initialN, delta = 0, bias = initialBias
        while handled < points.count {
            let m = points.filter { $0 >= n }.min()!
            delta += (m - n) * (handled + 1)
            n = m
            for p in points {
                if p < n { delta += 1 }
                if p == n {
                    var q = delta
                    var k = base
                    while true {
                        let t = k <= bias ? tMin : (k >= bias + tMax ? tMax : k - bias)
                        if q < t { break }
                        output.append(digit(t + (q - t) % (base - t)))
                        q = (q - t) / (base - t)
                        k += base
                    }
                    output.append(digit(q))
                    bias = adapt(delta, handled + 1, handled == basicCount)
                    delta = 0
                    handled += 1
                }
            }
            delta += 1
            n += 1
        }
        return output
    }
}
