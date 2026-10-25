package com.otodock;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

/**
 * Persistent registry of connected OtoDock server installations.
 *
 * Replaces the single {@code server_url} SharedPreferences string with a list of
 * installations plus an active pointer, so the app can hold and switch between
 * multiple servers. One install is the favorite (the cold-start target).
 *
 * Invariants (enforced on every load and mutation):
 * <ul>
 *   <li>exactly one favorite when the list is non-empty;</li>
 *   <li>activeId references an existing install (or null when the list is empty);</li>
 *   <li>no two installs share a host (same-host installs would collide on cookies);</li>
 *   <li>no two installs share a proxy installId.</li>
 * </ul>
 *
 * Stored as JSON under {@code otodock_prefs/installations}. A one-time migration
 * seeds the list from the legacy {@code otodock_prefs/server_url} key.
 */
public class InstallStore {

    private static final String PREFS_NAME = "otodock_prefs";
    private static final String KEY_INSTALLS = "installations";
    private static final String LEGACY_KEY_SERVER_URL = "server_url";
    private static final int VERSION = 1;

    public static class Install {
        public String id;
        public String url;
        public String label;
        public boolean favorite;
        public String installId;  // proxy install_id (relay identity); null until fetched
        public String lastRoute;  // app-shell path to restore on switch-in; null = root
        public long addedAt;
    }

    private final SharedPreferences prefs;
    private String activeId;
    private final List<Install> installs = new ArrayList<>();

    public InstallStore(Context ctx) {
        prefs = ctx.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
        load();
        migrateFromLegacy();
    }

    /** Lowercase hostname of a URL, with a tolerant fallback parse. */
    static String hostOf(String url) {
        if (url == null) return null;
        try {
            String h = Uri.parse(url).getHost();
            if (h != null) return h.toLowerCase();
        } catch (Exception ignored) {}
        String s = url.replace("https://", "").replace("http://", "");
        int slash = s.indexOf('/');
        if (slash >= 0) s = s.substring(0, slash);
        return s.toLowerCase();
    }

    private static String optStringOrNull(JSONObject o, String key) {
        if (!o.has(key) || o.isNull(key)) return null;
        return o.optString(key, null);
    }

    // --- persistence ---

    private synchronized void load() {
        installs.clear();
        activeId = null;
        String raw = prefs.getString(KEY_INSTALLS, null);
        if (raw == null) return;
        try {
            JSONObject root = new JSONObject(raw);
            activeId = optStringOrNull(root, "activeId");
            JSONArray arr = root.optJSONArray("installs");
            if (arr != null) {
                for (int i = 0; i < arr.length(); i++) {
                    JSONObject o = arr.getJSONObject(i);
                    Install in = new Install();
                    in.id = o.getString("id");
                    in.url = o.getString("url");
                    in.label = o.optString("label", hostOf(in.url));
                    in.favorite = o.optBoolean("favorite", false);
                    in.installId = optStringOrNull(o, "installId");
                    in.lastRoute = optStringOrNull(o, "lastRoute");
                    in.addedAt = o.optLong("addedAt", 0L);
                    installs.add(in);
                }
            }
        } catch (Exception ignored) {}
        enforceInvariants();
    }

    public synchronized void save() {
        try {
            JSONObject root = new JSONObject();
            root.put("version", VERSION);
            if (activeId != null) root.put("activeId", activeId);
            JSONArray arr = new JSONArray();
            for (Install in : installs) {
                JSONObject o = new JSONObject();
                o.put("id", in.id);
                o.put("url", in.url);
                o.put("label", in.label);
                o.put("favorite", in.favorite);
                if (in.installId != null) o.put("installId", in.installId);
                if (in.lastRoute != null) o.put("lastRoute", in.lastRoute);
                o.put("addedAt", in.addedAt);
                arr.put(o);
            }
            root.put("installs", arr);
            prefs.edit().putString(KEY_INSTALLS, root.toString()).apply();
        } catch (Exception ignored) {}
    }

    /** One-time seed of the list from the legacy single-server key. */
    private synchronized void migrateFromLegacy() {
        if (prefs.contains(KEY_INSTALLS)) return;
        String legacy = prefs.getString(LEGACY_KEY_SERVER_URL, null);
        if (legacy == null || legacy.isEmpty()) return;
        Install in = new Install();
        in.id = UUID.randomUUID().toString();
        in.url = legacy;
        in.label = hostOf(legacy);
        in.favorite = true;
        in.addedAt = System.currentTimeMillis();
        installs.add(in);
        activeId = in.id;
        save();
        prefs.edit().remove(LEGACY_KEY_SERVER_URL).apply();
    }

    // --- invariants ---

    private void enforceInvariants() {
        if (installs.isEmpty()) {
            activeId = null;
            return;
        }
        if (byIdInternal(activeId) == null) {
            Install fav = favoriteInternal();
            activeId = (fav != null) ? fav.id : installs.get(0).id;
        }
        int favCount = 0;
        for (Install in : installs) if (in.favorite) favCount++;
        if (favCount == 0) {
            installs.get(0).favorite = true;
        } else if (favCount > 1) {
            boolean kept = false;
            for (Install in : installs) {
                if (in.favorite) {
                    if (kept) in.favorite = false;
                    else kept = true;
                }
            }
        }
    }

    private Install byIdInternal(String id) {
        if (id == null) return null;
        for (Install in : installs) if (in.id.equals(id)) return in;
        return null;
    }

    private Install favoriteInternal() {
        for (Install in : installs) if (in.favorite) return in;
        return null;
    }

    // --- public API ---

    public synchronized List<Install> list() { return new ArrayList<>(installs); }
    public synchronized String activeIdValue() { return activeId; }
    public synchronized Install active() { return byIdInternal(activeId); }
    public synchronized Install favorite() { return favoriteInternal(); }
    public synchronized Install byId(String id) { return byIdInternal(id); }

    public synchronized Install byHost(String host) {
        if (host == null) return null;
        for (Install in : installs) {
            if (host.equalsIgnoreCase(hostOf(in.url))) return in;
        }
        return null;
    }

    public synchronized Install byInstallId(String pid) {
        if (pid == null) return null;
        for (Install in : installs) {
            if (pid.equals(in.installId)) return in;
        }
        return null;
    }

    /** Add a new install, or return the existing one on the same host. */
    public synchronized Install add(String url, String label) {
        Install existing = byHost(hostOf(url));
        if (existing != null) return existing;
        Install in = new Install();
        in.id = UUID.randomUUID().toString();
        in.url = url;
        in.label = (label != null && !label.isEmpty()) ? label : hostOf(url);
        in.favorite = installs.isEmpty();
        in.addedAt = System.currentTimeMillis();
        installs.add(in);
        if (activeId == null) activeId = in.id;
        enforceInvariants();
        save();
        return in;
    }

    public synchronized void remove(String id) {
        Install in = byIdInternal(id);
        if (in == null) return;
        installs.remove(in);
        if (id.equals(activeId)) activeId = null;  // reassigned by enforceInvariants
        enforceInvariants();
        save();
    }

    public synchronized void setActive(String id) {
        if (byIdInternal(id) == null) return;
        activeId = id;
        save();
    }

    public synchronized void setFavorite(String id) {
        if (byIdInternal(id) == null) return;
        for (Install in : installs) in.favorite = in.id.equals(id);
        save();
    }

    public synchronized void rename(String id, String label) {
        Install in = byIdInternal(id);
        String capped = NativeBridgePolicy.capLabel(label);
        if (in == null || capped == null) return;
        in.label = capped;
        save();
    }

    /** Bind the proxy install_id to an install. First-writer-wins on collisions. */
    public synchronized boolean setInstallId(String id, String pid) {
        Install in = byIdInternal(id);
        if (in == null || pid == null) return false;
        Install owner = byInstallId(pid);
        if (owner != null && owner != in) return false;  // uniqueness
        in.installId = pid;
        save();
        return true;
    }

    public synchronized void setLastRoute(String id, String path) {
        Install in = byIdInternal(id);
        if (in == null) return;
        in.lastRoute = path;
        save();
    }

    /** Public list for the dashboard Settings UI: id/url/label/favorite/active. */
    public synchronized String toPublicJson() {
        JSONArray arr = new JSONArray();
        try {
            for (Install in : installs) {
                JSONObject o = new JSONObject();
                o.put("id", in.id);
                o.put("url", in.url);
                o.put("label", in.label);
                o.put("favorite", in.favorite);
                o.put("active", in.id.equals(activeId));
                arr.put(o);
            }
        } catch (Exception ignored) {}
        return arr.toString();
    }
}
