package com.otodock;

import java.net.IDN;
import java.net.URI;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * The native bridge's rules, kept free of Android types so plain JUnit runs
 * them: which origin the dashboard channel serves, what a saved file may be,
 * which address the system browser may open, and when a deep link is taken.
 */
final class NativeBridgePolicy {

    /** Name of the origin-scoped object the dashboard posts on. */
    static final String CHANNEL = "OtoDockNative";

    static final int MAX_SAVE_BYTES = 25_000_000;
    // The base64 of MAX_SAVE_BYTES plus the JSON header, refused before any parsing.
    static final int MAX_MESSAGE_CHARS = 36 * 1024 * 1024;
    static final int MAX_LABEL_CODE_POINTS = 80;
    private static final int MAX_SAVE_NAME_CHARS = 100;

    /** OtoDock's privacy policy for the apps: the stores require a link to it
     *  inside the app. Fixed here, so no page can name another address. */
    static final String PRIVACY_POLICY_URL = "https://otodock.io/legal/privacy";

    /** How long a sign-in the app opened may take to come back through its deep link. */
    static final long AUTH_FLOW_TTL_MS = 10 * 60 * 1000L;
    static final String FLOW_AUTH = "auth";
    static final String FLOW_OAUTH = "oauth";

    // The lightbox's image types (ImageLightbox.tsx MIME_EXT) and the wake diagnostics.
    private static final Map<String, String> SAVE_TYPES = new HashMap<>();
    static {
        SAVE_TYPES.put("image/jpeg", "jpg");
        SAVE_TYPES.put("image/png", "png");
        SAVE_TYPES.put("image/gif", "gif");
        SAVE_TYPES.put("image/webp", "webp");
        SAVE_TYPES.put("image/svg+xml", "svg");
        SAVE_TYPES.put("image/heic", "heic");
        SAVE_TYPES.put("image/heif", "heif");
        SAVE_TYPES.put("image/avif", "avif");
        SAVE_TYPES.put("image/bmp", "bmp");
        SAVE_TYPES.put("image/tiff", "tiff");
        SAVE_TYPES.put("application/json", "json");
    }

    private NativeBridgePolicy() {}

    // --- origins ---

    /** {@code scheme://host[:port]} of an http(s) address with a host and no
     *  user info, the host in lower-case ASCII (an IPv6 literal bracketed in
     *  the form the WebView reports origins in) and the port only when it is
     *  not the scheme's default; null for anything else. The path is ignored. */
    static String origin(String url) {
        URI uri = parse(url);
        if (uri == null) return null;
        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase(Locale.ROOT);
        if (!scheme.equals("https") && !scheme.equals("http")) return null;
        String host = asciiHost(uri);
        if (host == null || uri.getRawUserInfo() != null) return null;
        int port = port(uri);
        if (port == BAD_PORT) return null;
        boolean defaultPort = port == -1
                || (scheme.equals("https") && port == 443)
                || (scheme.equals("http") && port == 80);
        return scheme + "://" + host + (defaultPort ? "" : ":" + port);
    }

    static boolean sameOrigin(String a, String b) {
        String oa = origin(a);
        return oa != null && oa.equals(origin(b));
    }

    /** Host part of an origin produced by {@link #origin}. */
    static String host(String origin) {
        URI uri = parse(origin);
        return uri == null ? null : asciiHost(uri);
    }

    /** False for the API prefix: no dashboard page lives there, only API
     *  answers and served content. */
    static boolean isDashboardPage(String topUrl) {
        URI uri = parse(topUrl);
        if (uri == null) return false;
        String path = uri.getRawPath();
        return path == null || !path.startsWith("/v1/");
    }

    private static URI parse(String url) {
        if (url == null || url.indexOf('\\') >= 0) return null;
        try {
            return new URI(url.trim());
        } catch (Exception e) {
            return null;
        }
    }

    private static final int BAD_PORT = -2;
    private static final Pattern AUTHORITY_PORT = Pattern.compile(":(\\d*)$");
    // An ASCII host name: letters, digits, dots, hyphens and underscores.
    private static final Pattern HOST_NAME = Pattern.compile("[a-z0-9._-]+");
    private static final Pattern HEX_PIECE = Pattern.compile("[0-9a-f]{1,4}");
    private static final Pattern IPV4_OCTET = Pattern.compile("0|[1-9][0-9]{0,2}");
    private static final Pattern SCHEME_PREFIX = Pattern.compile("[A-Za-z][A-Za-z0-9+.-]*://");

    /** The port, -1 for none, BAD_PORT for one no URL can carry. java.net.URI
     *  leaves a host it cannot parse as a server name (a non-ASCII or an
     *  underscore host) in the authority, port included, and parses a server
     *  host's port with no range check. */
    private static int port(URI uri) {
        if (uri.getHost() != null) {
            int port = uri.getPort();
            return port <= 65535 ? port : BAD_PORT;
        }
        String authority = uri.getRawAuthority();
        Matcher m = authority == null ? null : AUTHORITY_PORT.matcher(authority);
        if (m == null || !m.find() || m.group(1).isEmpty()) return -1;
        String digits = m.group(1);
        if (digits.length() > 5) return BAD_PORT;
        int port = Integer.parseInt(digits);
        return port <= 65535 ? port : BAD_PORT;
    }

    private static String asciiHost(URI uri) {
        String host = uri.getHost();
        if (host == null) {
            String authority = uri.getRawAuthority();
            if (authority == null || authority.contains("@")) return null;
            host = AUTHORITY_PORT.matcher(authority).replaceFirst("");
        }
        if (host.isEmpty()) return null;
        if (host.startsWith("[")) return ipv6Literal(host);
        try {
            // Without the STD3 rules, which refuse "_"; the name check keeps
            // every other character they refused out.
            String ascii = IDN.toASCII(host).toLowerCase(Locale.ROOT);
            return HOST_NAME.matcher(ascii).matches() ? ascii : null;
        } catch (Exception e) {
            return null;
        }
    }

    /** A bracketed IPv6 literal in the URL Standard's serialisation, the form
     *  the WebView reports an origin in (lower-case hex without leading zeros,
     *  the first longest run of two or more zero pieces as "::", an IPv4 tail
     *  as two hex pieces); null for anything else, a zone id included. Parsed
     *  by hand: InetAddress looks a malformed literal up as a name. */
    static String ipv6Literal(String bracketed) {
        if (bracketed.length() < 3 || bracketed.charAt(0) != '['
                || bracketed.charAt(bracketed.length() - 1) != ']') return null;
        int[] pieces = ipv6Pieces(bracketed.substring(1, bracketed.length() - 1).toLowerCase(Locale.ROOT));
        if (pieces == null) return null;
        int runStart = -1, runLength = 0;
        for (int i = 0; i < 8; ) {
            if (pieces[i] != 0) { i++; continue; }
            int j = i;
            while (j < 8 && pieces[j] == 0) j++;
            if (j - i > runLength) { runStart = i; runLength = j - i; }
            i = j;
        }
        if (runLength < 2) runStart = -1;
        StringBuilder out = new StringBuilder("[");
        for (int i = 0; i < 8; i++) {
            if (i == runStart) {
                out.append("::");
                i += runLength - 1;
                continue;
            }
            if (out.length() > 1 && out.charAt(out.length() - 1) != ':') out.append(':');
            out.append(Integer.toHexString(pieces[i]));
        }
        return out.append(']').toString();
    }

    /** The eight 16-bit pieces of an IPv6 address, or null when malformed. */
    private static int[] ipv6Pieces(String address) {
        int gap = address.indexOf("::");
        if (gap >= 0 && address.indexOf("::", gap + 1) >= 0) return null;
        String head = gap >= 0 ? address.substring(0, gap) : address;
        String tail = gap >= 0 ? address.substring(gap + 2) : "";
        // A dotted IPv4 tail is allowed only at the very end of the address.
        int[] h = hexPieces(head, gap < 0);
        int[] t = hexPieces(tail, true);
        if (h == null || t == null) return null;
        if (gap < 0 ? h.length != 8 : h.length + t.length > 7) return null;
        int[] pieces = new int[8];
        System.arraycopy(h, 0, pieces, 0, h.length);
        System.arraycopy(t, 0, pieces, 8 - t.length, t.length);
        return pieces;
    }

    private static int[] hexPieces(String part, boolean ipv4TailAllowed) {
        if (part.isEmpty()) return new int[0];
        String[] groups = part.split(":", -1);
        String last = groups[groups.length - 1];
        boolean ipv4 = last.indexOf('.') >= 0;
        if (ipv4 && !ipv4TailAllowed) return null;
        int[] out = new int[groups.length + (ipv4 ? 1 : 0)];
        for (int i = 0; i < groups.length - (ipv4 ? 1 : 0); i++) {
            if (!HEX_PIECE.matcher(groups[i]).matches()) return null;
            out[i] = Integer.parseInt(groups[i], 16);
        }
        if (ipv4) {
            String[] octets = last.split("\\.", -1);
            if (octets.length != 4) return null;
            int[] b = new int[4];
            for (int i = 0; i < 4; i++) {
                if (!IPV4_OCTET.matcher(octets[i]).matches()) return null;
                b[i] = Integer.parseInt(octets[i]);
                if (b[i] > 255) return null;
            }
            out[out.length - 2] = b[0] << 8 | b[1];
            out[out.length - 1] = b[2] << 8 | b[3];
        }
        return out;
    }

    // --- server addresses ---

    /** The address a typed server URL is saved as: trimmed, one trailing slash
     *  dropped, https added to a bare host. Null for another scheme, for no
     *  origin (see {@link #origin}) and for plain http to a host
     *  {@link #cleartextHost} does not allow, so an address the app cannot
     *  boot is never saved and a public host never loads in cleartext. */
    static String serverUrl(String raw) {
        if (raw == null) return null;
        String url = raw.trim();
        if (url.isEmpty()) return null;
        if (url.endsWith("/")) url = url.substring(0, url.length() - 1);
        boolean http = url.regionMatches(true, 0, "http://", 0, 7);
        if (!http && !url.regionMatches(true, 0, "https://", 0, 8)) {
            if (SCHEME_PREFIX.matcher(url).lookingAt()) return null;
            url = "https://" + url;
        }
        String origin = origin(url);
        if (origin == null) return null;
        if (http && !cleartextHost(host(origin))) return null;
        return url;
    }

    /** Whether the app may boot an origin produced by {@link #origin}: https,
     *  or plain http to a host {@link #cleartextHost} allows. Saved installs
     *  are read through it too, so one saved under an older, looser rule
     *  does not load a public address in cleartext. */
    static boolean bootable(String origin) {
        if (origin == null) return false;
        if (origin.startsWith("https://")) return true;
        return origin.startsWith("http://") && cleartextHost(host(origin));
    }

    /** The hosts plain http may reach, as {@link #host} gives them: an IPv4
     *  address in 10/8, 172.16/12, 192.168/16, 127/8 or 169.254/16; an IPv6
     *  address that is ::1 or in fc00::/7 or fe80::/10, or an IPv4-mapped one
     *  under the IPv4 rule; "localhost" and a ".local" name. An octet with a
     *  leading zero is refused: the WebView reads it as octal, so "0127.x.y.z"
     *  is not the loopback address it looks like. */
    static boolean cleartextHost(String host) {
        if (host == null) return false;
        String h = host.toLowerCase(Locale.ROOT);
        if (h.equals("localhost") || h.endsWith(".local")) return true;
        if (h.startsWith("[")) {
            int[] p = h.length() > 2 && h.endsWith("]") ? ipv6Pieces(h.substring(1, h.length() - 1)) : null;
            if (p == null) return false;
            if (zeroPieces(p, 7) && p[7] == 1) return true;                     // ::1
            if ((p[0] & 0xfe00) == 0xfc00 || (p[0] & 0xffc0) == 0xfe80) return true;
            return zeroPieces(p, 5) && p[5] == 0xffff
                    && privateIpv4(p[6] >> 8, p[6] & 0xff);
        }
        String[] parts = h.split("\\.", -1);
        if (parts.length != 4) return false;
        int[] o = new int[4];
        for (int i = 0; i < 4; i++) {
            if (!IPV4_OCTET.matcher(parts[i]).matches()) return false;
            o[i] = Integer.parseInt(parts[i]);
            if (o[i] > 255) return false;
        }
        return privateIpv4(o[0], o[1]);
    }

    private static boolean zeroPieces(int[] pieces, int count) {
        for (int i = 0; i < count; i++) {
            if (pieces[i] != 0) return false;
        }
        return true;
    }

    /** By the first two octets of an IPv4 address. */
    private static boolean privateIpv4(int a, int b) {
        return a == 10 || (a == 172 && b >= 16 && b <= 31) || (a == 192 && b == 168)
                || a == 127 || (a == 169 && b == 254);
    }

    // --- messages ---

    /** A message is a JSON header, optionally followed by one newline and a raw
     *  body (JSON.stringify never emits a raw newline, so the first one splits). */
    static String[] splitMessage(String data) {
        int nl = data.indexOf('\n');
        if (nl < 0) return new String[]{data, null};
        return new String[]{data.substring(0, nl), data.substring(nl + 1)};
    }

    // --- saving files ---

    static String normalizeMime(String mime) {
        if (mime == null) return null;
        String m = mime.trim();
        int semi = m.indexOf(';');
        if (semi >= 0) m = m.substring(0, semi).trim();
        m = m.toLowerCase(Locale.ROOT);
        return m.equals("image/jpg") ? "image/jpeg" : m;
    }

    /** The file extension for a type the app saves, or null when refused. */
    static String saveExtension(String mime) {
        String m = normalizeMime(mime);
        return m == null ? null : SAVE_TYPES.get(m);
    }

    /** The requested name's last path segment without its extension, reduced
     *  to plain characters, with the extension of the type. */
    static String saveFileName(String requested, String extension) {
        String base = requested == null ? "" : requested;
        base = base.substring(Math.max(base.lastIndexOf('/'), base.lastIndexOf('\\')) + 1);
        base = base.replaceFirst("^[.\\s]+", "");
        int dot = base.lastIndexOf('.');
        if (dot > 0) base = base.substring(0, dot);
        base = base.replaceAll("[^A-Za-z0-9 _.-]", "_").trim();
        if (base.length() > MAX_SAVE_NAME_CHARS) base = base.substring(0, MAX_SAVE_NAME_CHARS);
        if (base.isEmpty()) base = "image";
        return base + "." + extension;
    }

    /** Whether a base64 payload of this length can decode within the cap. */
    static boolean saveBase64Fits(int chars) {
        return (long) chars * 3 / 4 <= MAX_SAVE_BYTES + 2;
    }

    // --- the switcher ---

    static String capLabel(String label) {
        if (label == null) return null;
        String l = label.trim();
        if (l.isEmpty()) return null;
        if (l.codePointCount(0, l.length()) <= MAX_LABEL_CODE_POINTS) return l;
        return l.substring(0, l.offsetByCodePoints(0, MAX_LABEL_CODE_POINTS));
    }

    // --- the system browser ---

    /** The address to hand the system browser, re-serialised from the parsed
     *  form: https only, a host, no user info; null otherwise. */
    static String authBrowserUrl(String url) {
        URI uri = parse(url);
        if (uri == null || !"https".equalsIgnoreCase(uri.getScheme())) return null;
        if (asciiHost(uri) == null || uri.getRawUserInfo() != null) return null;
        return uri.toASCIIString();
    }

    /** True when the address is not on the active install's origin, so the app
     *  names the other site and asks before opening it. */
    static boolean needsHostConfirm(String url, String activeOrigin) {
        String target = origin(url);
        return target == null || !target.equals(activeOrigin);
    }

    static boolean isFlowKind(String kind) {
        return FLOW_AUTH.equals(kind) || FLOW_OAUTH.equals(kind);
    }

    // --- the deep link ---

    /** Whether a flow of {@code linkKind} is armed and within its window. A
     *  deadline further ahead than the window reads as expired (the clock it was
     *  taken on restarted). */
    static boolean flowPending(String armedKind, long until, String linkKind, long now) {
        return armedKind != null && armedKind.equals(linkKind)
                && until > now && until - now <= AUTH_FLOW_TTL_MS;
    }

    /** The local id of the install a deep link goes to, or null to drop it:
     *  only while a flow of its kind is in flight, only to the install that
     *  started it (a tag naming another known install drops it). */
    static String deepLinkTarget(String linkKind, String armedKind, long until, long now,
                                 String initiatingId, String taggedId) {
        if (!flowPending(armedKind, until, linkKind, now) || initiatingId == null) return null;
        if (taggedId != null && !taggedId.equals(initiatingId)) return null;
        return initiatingId;
    }
}
