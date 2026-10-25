package com.otodock;

import android.app.ActivityManager;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.ContentValues;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ActivityInfo;
import android.content.pm.PackageManager;
import android.content.res.Configuration;
import android.media.AudioAttributes;
import android.media.MediaScannerConnection;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.os.SystemClock;
import android.provider.MediaStore;
import android.provider.Settings;
import android.util.Base64;
import android.graphics.Color;
import android.view.MotionEvent;
import android.view.OrientationEventListener;
import android.view.View;
import android.view.Window;
import android.animation.ArgbEvaluator;
import android.animation.ValueAnimator;
import android.graphics.drawable.GradientDrawable;
import android.util.Log;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.URLUtil;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import android.widget.Toast;
import androidx.activity.OnBackPressedCallback;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.core.view.WindowInsetsControllerCompat;
import androidx.core.graphics.ColorUtils;
import androidx.core.splashscreen.SplashScreen;
import androidx.core.graphics.Insets;
import androidx.lifecycle.DefaultLifecycleObserver;
import androidx.lifecycle.LifecycleOwner;
import androidx.webkit.JavaScriptReplyProxy;
import androidx.webkit.WebMessageCompat;
import androidx.webkit.WebMessagePortCompat;
import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;
import com.getcapacitor.BridgeActivity;
import com.getcapacitor.BridgeWebViewClient;
import com.getcapacitor.CapConfig;
import com.getcapacitor.WebViewListener;
import com.google.firebase.FirebaseApp;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

public class MainActivity extends BridgeActivity {

    private static final String TAG = "OtoDock";
    private static final String SETUP_PAGE = "file:///android_asset/public/setup.html";

    // --- The native bridge ---
    // The dashboard reaches native code only through NativeBridgePolicy.CHANNEL, a
    // WebMessageListener whose object exists only in frames of the active install's
    // exact origin; each message is judged on its own sender (origin, main frame).
    // The one raw interface left (SetupBridge) serves the bundled setup page.
    private volatile String activeOrigin = null;
    // Channel calls run here one at a time, in arrival order: the dashboard
    // posts a refresh after a change and counts on it running second.
    private static final ExecutorService NATIVE_CALLS = Executors.newSingleThreadExecutor(r -> {
        Thread t = new Thread(r, "OtoDockNative");
        t.setDaemon(true);
        return t;
    });
    private static final String WEBVIEW_TOO_OLD =
            "OtoDock needs a newer Android System WebView. Update Android System WebView "
                    + "from the Play Store, then open the app again.";
    private static final String ADDRESS_NOT_USABLE =
            "This server address can't be opened in the app. Switch to another server, "
                    + "or add it again by its https address.";

    // The sign-in the app opened in the system browser (openAuthBrowser): the deep
    // link that ends it is taken only while it is in flight, only for its kind
    // (auth / oauth) and only for the install that started it. Persisted, so it
    // survives the process being killed while the browser is in front.
    private static final String AUTH_FLOW_INSTALL = "auth_initiating_install";
    private static final String AUTH_FLOW_KIND = "auth_flow_kind";
    private static final String AUTH_FLOW_UNTIL = "auth_flow_until";

    // --- Server installations ---
    // The registry of connected servers + active pointer. A switch rebuilds the
    // Capacitor Bridge (server.url / allowNavigation are frozen at Bridge build),
    // so switchInstallation/addInstallation/removeInstallation all recreate() the
    // activity; sSwitchInProgress lets onCreate skip the boot hold and the prompts.
    private InstallStore installStore;
    private static boolean sSwitchInProgress = false;
    // False until the first load() of this process. A genuine cold start (not a
    // switch-recreate, not a notification/deep-link launch) opens the favorite.
    private static boolean sColdStarted = false;
    // The launch intent (e.g. an OAuth deep link) must be routed ONCE per process.
    // recreate() preserves the launch intent, so without this guard every install
    // switch would re-process it and yank back to the launch's install.
    private static boolean sLaunchRoutingConsumed = false;
    // The top-level URL of the last finished page load: the setup object's gate
    // (read off the UI thread, so not getUrl()). Cleared before any main-frame
    // navigation away, so a document still loading never inherits the setup page.
    private volatile String currentLoadedUrl = null;
    // True once the loaded SPA has signalled dashboardReady(); reset each load().
    // Gates OAuth deep-link delivery so a callback is never fired before the page
    // (and its window._handleDeepLink) exists.
    private volatile boolean dashboardIsReady = false;
    // The system splash is held until the dashboard's first real screen (the
    // channel method firstScreen), never on a recreate (no splash shows) and
    // at most BOOT_HOLD_MAX_MS once the WebView has committed a page, so a
    // server that never signals (one older than the boot screen) is not held
    // for ever. A WebView with no page yet (a cold process on a slow link
    // takes about 3 s to its first paint) keeps the splash up to
    // BOOT_HOLD_CAP_MS: a fade onto the bare field would drop the mark and
    // bring it back. The keep condition is read on every draw pass (a
    // cancelled pre-draw reschedules a traversal).
    private static final long BOOT_HOLD_MAX_MS = 2500L;
    private static final long BOOT_HOLD_CAP_MS = 8000L;
    private static final long BOOT_VISUAL_WAIT_MS = 150L;
    private static final long BOOT_FADE_MS = 250L;
    private static final long CONTAINER_FADE_MS = 200L;
    private volatile boolean bootHeld = false;
    private volatile boolean pageCommitted = false;
    private long bootStart = 0L;
    // Whether a system splash is expected over this activity, whether the
    // system began handing it over, whether its view was removed, and whether
    // the page was told it is gone: the page's boot screen fades only then.
    private boolean splashExpected = false;
    private boolean splashLeaving = false;
    private boolean splashRemoved = false;
    private volatile boolean splashGone = false;
    private boolean startupQuestionsWaiting = false;
    // The container's two gradient stops and the bars as last painted, so the
    // state survives the splash's removal and a configuration change (both
    // re-apply the theme's own).
    private final int[] containerColors = new int[2];
    private ValueAnimator containerFade;
    private int barStatusColor;
    private int barNavigationColor;
    private boolean barLight;
    // The bar insets last pushed to the page, in physical px (-1: none yet).
    private int barTopPx = -1;
    private int barBottomPx = -1;
    // Set while an auth flow (system browser / Custom Tab / device-code) is in
    // progress, to block user-initiated install switches mid-flow. Instance-scoped:
    // survives the browser round-trip (onNewIntent), cleared on delivery/timeout.
    private volatile boolean authInProgress = false;
    private long authInProgressUntil = 0L;
    // Set by the dashboard when live work (voice/meeting/upload/dirty form) would be
    // lost on a switch. Combined with isStreaming for the switch busy-guard.
    private volatile boolean switchBusyFlag = false;
    // 3-finger horizontal swipe tracking (install switcher).
    private boolean swipeArmed = false;
    // The fold log (logcat tag OtoDockFold): the display switch of a foldable,
    // read from a capture. Attached once the bridge's WebView exists; the
    // channel thread reads it too (a page's note), so it is read once there.
    private volatile FoldDiagnostics foldDiag;
    private float swipeStartX, swipeStartY, swipeLastX, swipeLastY;

    private static final String AUX_PREFS = "otodock_prefs";

    private void setAuxString(String key, String val, boolean sync) {
        SharedPreferences.Editor e = getSharedPreferences(AUX_PREFS, MODE_PRIVATE).edit().putString(key, val);
        if (sync) e.commit(); else e.apply();
    }

    private String takeAuxString(String key) {
        SharedPreferences p = getSharedPreferences(AUX_PREFS, MODE_PRIVATE);
        String v = p.getString(key, null);
        if (v != null) p.edit().remove(key).apply();
        return v;
    }

    /** The host the WebView reports for the booted install (punycode for an
     *  IDN host, a bracketed canonical IPv6 literal), null on the setup page.
     *  Compare page and request hosts with this, never with the saved URL's
     *  host as typed. */
    private String activeHost() {
        String origin = activeOrigin;
        return origin == null ? null : NativeBridgePolicy.host(origin);
    }

    /** Extract hostname from URL (e.g., "https://x.example.com" -> "x.example.com") */
    private String extractHost(String url) {
        try {
            return new java.net.URL(url).getHost();
        } catch (Exception e) {
            return url.replace("https://", "").replace("http://", "").split("/")[0];
        }
    }

    /**
     * Override Capacitor's load() to inject a dynamic server URL from SharedPreferences.
     *
     * BridgeActivity has `protected CapConfig config;` which is passed to
     * `bridgeBuilder.setConfig(config).create()` in super.load(). We set it here
     * so the Bridge initializes with the user's chosen server URL.
     *
     * If no URL is saved, we let the Bridge load local assets (no server.url in
     * capacitor.config.json) and then redirect to setup.html.
     */
    @Override
    protected void load() {
        // Constructed here (during super.onCreate) so the legacy server_url
        // migration runs before the active install is read.
        if (installStore == null) installStore = new InstallStore(this);
        dashboardIsReady = false;
        activeOrigin = null;
        // Registered after the plugins of capacitor.plugins.json (added in
        // BridgeActivity.onCreate), so it replaces their push plugin.
        if (!firebaseConfigured()) registerPlugin(PushUnconfiguredPlugin.class);

        // On a genuine process cold start (not a switch-recreate, not a launch that
        // targets a specific install via notification/deep-link), open the favorite.
        boolean firstLoad = !sColdStarted;
        sColdStarted = true;
        if (firstLoad && !sSwitchInProgress && !isRoutedLaunch(getIntent())) {
            InstallStore.Install fav = installStore.favorite();
            if (fav != null) installStore.setActive(fav.id);
        }

        InstallStore.Install active = installStore.active();
        String origin = active != null ? NativeBridgePolicy.origin(active.url) : null;
        // Fail closed: without the scoped channel nothing remote boots (there is
        // no fallback to a raw interface).
        String blocked = null;
        if (active != null) {
            if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
                blocked = WEBVIEW_TOO_OLD;
            } else if (!NativeBridgePolicy.bootable(origin)) {
                blocked = ADDRESS_NOT_USABLE;
            }
        }
        if (active != null && blocked == null) {
            // Boot the active server. The server URL is the bare origin: Capacitor
            // copies it into its own plugin bridge's origin rules, where a path is
            // invalid and makes it fall back to a raw interface in every frame; a
            // route rides the start path instead. allowNavigation is scoped to the
            // EXACT active host (no wildcard): a wildcard parent (e.g. *.example.org)
            // would let a sibling subdomain load in-app with Capacitor's bridge.
            String startPath = null;
            // The saved route belongs to the switch that wrote it (a switch-away
            // keeps the page it leaves, a notification tap the page it targets)
            // and is spent by the recreate that follows: a switch-in reopens
            // there, every other load — a cold start above all — boots at the
            // root, where the SPA opens the user's favorite agent. Kept, one
            // stale route would reopen on every cold start.
            if (active.lastRoute != null) {
                if (sSwitchInProgress && isSafeRoutePath(active.lastRoute)) {
                    startPath = active.lastRoute;
                }
                installStore.setLastRoute(active.id, null);
            }

            activeOrigin = origin;
            this.config = new CapConfig.Builder(this)
                    .setServerUrl(origin)
                    .setStartPath(startPath)
                    .setAllowNavigation(new String[]{ NativeBridgePolicy.host(origin) })
                    .setPluginsConfiguration(pluginsConfiguration())
                    .create();
            super.load();
        } else {
            // No usable install: let Bridge init with local assets, then show setup.
            super.load();
            if (blocked == null) {
                loadSetupPage("");
            } else {
                // The picker on the "unreachable" page offers the other installs.
                loadSetupPage(blocked.equals(WEBVIEW_TOO_OLD)
                        ? "" : "error=unreachable&url=" + Uri.encode(active.url));
                final String message = blocked;
                getBridge().getWebView().post(() -> showBridgeBlockedDialog(message));
            }
        }
    }

    /**
     * The plugins block of capacitor.config.json, which cap sync writes into
     * the assets from capacitor.config.ts. The setup page boots through
     * Capacitor's own CapConfig.loadDefault, which reads the file; a server
     * boot builds its config here, and the plugins must read the same values
     * on both: above all SystemBars' insetsHandling, which keeps Capacitor from
     * padding the window for the keyboard a second time (the container
     * listener in onCreate is the one source of that padding). A missing or
     * unreadable file leaves the plugins on their defaults.
     */
    private JSONObject pluginsConfiguration() {
        try (InputStream in = getAssets().open("capacitor.config.json")) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            return new JSONObject(out.toString(StandardCharsets.UTF_8.name())).optJSONObject("plugins");
        } catch (IOException | JSONException e) {
            Log.w(TAG, "capacitor.config.json not read, plugins on their defaults: " + e.getMessage());
            return null;
        }
    }

    /** Whether the build carries a Firebase project (google-services.json):
     *  its content provider then starts the default Firebase app. */
    private static boolean firebaseConfigured() {
        try {
            FirebaseApp.getInstance();
            return true;
        } catch (IllegalStateException e) {
            return false;
        }
    }

    /** True when the launch intent targets a specific install (a notification tap
     *  or the deep link of a sign-in in flight): those route themselves, so a cold
     *  start must not override the active install to the favorite. */
    private boolean isRoutedLaunch(Intent i) {
        if (i == null) return false;
        if (Intent.ACTION_VIEW.equals(i.getAction())) {
            Uri d = i.getData();
            if (d != null && "otodock".equals(d.getScheme())) return deepLinkTarget(d) != null;
        }
        return i.getStringExtra("install_id") != null
                || i.getStringExtra("google.message_id") != null;
    }

    // Common multi-label public suffixes. Without a full Public Suffix List we use
    // this denylist so a host sitting directly under one (e.g. "myserver.co.uk")
    // doesn't treat every "*.co.uk" host as a forward-auth sibling. Installs on a
    // suffix not listed here simply open the redirect in the external browser (safe),
    // not in-WebView. (Deeper structures like "app.company.co.uk" still work — their
    // computed parent "company.co.uk" is not a public suffix.)
    private static final java.util.Set<String> MULTI_LABEL_PUBLIC_SUFFIXES =
        new java.util.HashSet<>(java.util.Arrays.asList(
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
            // Europe second-level suffixes. (Bare single-label TLDs — .de, .fr, .it,
            // .gr, .es — are registrable directly: "yourserver.de" has one dot and is
            // already covered by the ">= 2 dots" rule above, so they need no entry.)
            "com.gr", "net.gr", "org.gr", "edu.gr", "gov.gr",
            "com.es", "org.es", "nom.es", "gob.es", "edu.es",
            "com.pl", "net.pl", "org.pl", "com.ru", "net.ru", "org.ru",
            "co.at", "or.at"));

    /** True when {@code uri} is an HTTPS main-frame target on a SIBLING host of the
     *  active install's parent domain (active {@code app.example.com} ->
     *  {@code auth.example.com}). Used to keep an Authentik forward-auth redirect IN
     *  the WebView (so its cookie persists and it can bounce back to the active host)
     *  WITHOUT adding the host to allowNavigation -- which would expose Capacitor's
     *  plugin bridge to it (the dashboard channel's origin rule never names it).
     *  Its scope is the one a {@code *.<parent>} navigation wildcard would give, minus
     *  the bridges. Bare registrable domains / IPs / LAN hosts have no
     *  siblings (require a real subdomain: >=2 dots and an alphabetic last label). */
    private boolean isAuthSiblingHost(Uri uri) {
        if (uri == null || installStore == null) return false;
        if (!"https".equalsIgnoreCase(uri.getScheme())) return false;
        String reqHost = uri.getHost();
        String activeHost = activeHost();
        if (reqHost == null || activeHost == null || reqHost.equalsIgnoreCase(activeHost)) return false;
        int firstDot = activeHost.indexOf('.');
        // Need a real subdomain (>=2 dots) and a DNS host (alphabetic TLD, so an
        // IPv4 literal -- which ends in a digit -- is excluded; it has no siblings).
        if (firstDot <= 0 || activeHost.indexOf('.', firstDot + 1) < 0) return false;
        if (Character.isDigit(activeHost.charAt(activeHost.length() - 1))) return false;
        String parentSuffix = activeHost.substring(firstDot).toLowerCase();  // ".example.com"
        // If the computed parent IS itself a public suffix (e.g. "co.uk"), every host
        // under it would falsely look like a sibling — refuse (no in-WebView sibling).
        if (MULTI_LABEL_PUBLIC_SUFFIXES.contains(parentSuffix.substring(1))) return false;
        return reqHost.toLowerCase().endsWith(parentSuffix);
    }

    /** A click_url / route is only safe to append to a server origin if it is a
     *  plain absolute path (single leading slash, no scheme/host, no control or
     *  line-separator chars that could break out of a URL or JS string). */
    private boolean isSafeRoutePath(String p) {
        if (p == null || p.isEmpty()) return false;
        if (!p.startsWith("/") || p.startsWith("//")) return false;
        if (p.contains("://") || p.contains("\\")) return false;
        for (int i = 0; i < p.length(); i++) {
            char c = p.charAt(i);
            if (c <= 0x20 || c == 0x7f || c == '\u2028' || c == '\u2029') return false;
        }
        return true;
    }

    /** Reduce a possibly-hostile click_url to a safe absolute path, or "/". */
    private String sanitizeClickUrl(String raw) {
        if (raw == null) return "/";
        String p = raw.trim();
        return isSafeRoutePath(p) ? p : "/";
    }

    /**
     * Fetch this proxy's install_id once authenticated and bind it to the active
     * install (H3 — native-fetched, not trusted from JS). Idempotent: a no-op once
     * bound, or until the user is logged in (the endpoint is auth-gated) — retried
     * on each page load until it succeeds.
     */
    private void maybeBindInstallId() {
        final InstallStore.Install active = installStore != null ? installStore.active() : null;
        if (active == null || active.installId != null) return;
        final String cur = currentLoadedUrl;
        final String origin = activeOrigin;
        if (cur == null || origin == null || !NativeBridgePolicy.sameOrigin(origin, active.url)) return;
        String curHost = Uri.parse(cur).getHost();
        if (curHost == null || !curHost.equalsIgnoreCase(activeHost())) return;

        final String localId = active.id;
        final String url = origin + "/v1/app/identity";
        final String cookie = CookieManager.getInstance().getCookie(origin);
        new Thread(() -> {
            try {
                java.net.HttpURLConnection conn = (java.net.HttpURLConnection)
                        new java.net.URL(url).openConnection();
                conn.setConnectTimeout(8000);
                conn.setReadTimeout(8000);
                conn.setRequestMethod("GET");
                if (cookie != null) conn.setRequestProperty("Cookie", cookie);
                int code = conn.getResponseCode();
                if (code == 200) {
                    java.io.BufferedReader r = new java.io.BufferedReader(
                            new java.io.InputStreamReader(conn.getInputStream(), "UTF-8"));
                    StringBuilder sb = new StringBuilder();
                    String line;
                    while ((line = r.readLine()) != null) sb.append(line);
                    r.close();
                    String pid = new JSONObject(sb.toString()).optString("install_id", "");
                    if (!pid.isEmpty()) installStore.setInstallId(localId, pid);
                }
                conn.disconnect();
            } catch (Exception ignored) {}
        }).start();
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        // Built into the Bridge with the app's plugins: its load() runs after
        // Capacitor's core plugins and before the first page load, where
        // onBridgeWebViewReady settles the bridge surface.
        registerPlugin(NativeChannelPlugin.class);
        installBootSplash(savedInstanceState);
        super.onCreate(savedInstanceState);
        if (getBridge() == null) {
            // BridgeActivity fell back to its no-WebView layout: nothing below has
            // a WebView to work on.
            showBridgeBlockedDialog(WEBVIEW_TOO_OLD);
            return;
        }

        // A switch between installs rebuilds the activity via recreate() (the
        // server URL + allowNavigation are frozen at Bridge build). Skip the
        // cold-start-only channel (re)creation and permission prompts so a
        // switch is quick and doesn't churn channels or re-prompt.
        boolean switching = sSwitchInProgress;
        sSwitchInProgress = false;

        if (!switching) {
            // Create notification channels for each severity level
            createNotificationChannels();
        }

        // Back-navigation via the AndroidX OnBackPressedDispatcher. Works for
        // both the legacy back button and the Android 13+ predictive back
        // gesture (including the far-edge swipe). The legacy onBackPressed()
        // override is NOT invoked when the system routes through the
        // OnBackInvokedDispatcher (the default on targetSdk 36), so without
        // this callback the predictive-back gesture falls through to the
        // system default and closes the activity.
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override
            public void handleOnBackPressed() {
                handleBackAction();
            }
        });

        // Streaming-aware background keep-alive (the bridge itself is set up in
        // onBridgeWebViewReady, before the first page load)
        setupBackgroundKeepAlive();
        foldDiag = FoldDiagnostics.attach(this, getBridge().getWebView(),
                savedInstanceState != null, this::foldFlags);

        // Ensure cookies persist across app restarts
        CookieManager cookieManager = CookieManager.getInstance();
        cookieManager.setAcceptCookie(true);
        cookieManager.setAcceptThirdPartyCookies(getBridge().getWebView(), true);

        // Flush cookies after every page load; track the current top-level URL so
        // off-UI-thread bridge calls can be origin-gated against the active install.
        getBridge().addWebViewListener(new WebViewListener() {
            @Override
            public void onPageCommitVisible(WebView view, String url) {
                pageCommitted = true;
                // A page that commits after the deadline releases at once: the
                // keep condition is read on a draw pass, and the hold has none.
                if (bootHeld && SystemClock.uptimeMillis() - bootStart >= BOOT_HOLD_MAX_MS) releaseBoot("deadline");
                // The earlier push may have landed in no document; a document
                // that paints with no splash over it (a recreate, or after the
                // splash left) learns so now.
                pushBarInsets(view, barTopPx, barBottomPx, true);
                if (!splashExpected || splashGone) tellSplash(view, "gone");
            }

            @Override
            public void onPageLoaded(WebView view) {
                CookieManager.getInstance().flush();
                currentLoadedUrl = view.getUrl();
                // The bundled page is its own first screen, on the phone's theme.
                if (currentLoadedUrl != null && currentLoadedUrl.startsWith(SETUP_PAGE)) {
                    paintSetupPage();
                    releaseBoot("setup");
                }
                maybeBindInstallId();
                renderSetupPicker(view);
            }
        });

        // Error recovery — only bounce to setup.html on a genuine "the
        // saved server URL is unreachable" failure.
        //
        // Background: Capacitor's WebViewListener.onReceivedError fires for
        // ANY WebView error — sub-resource (XHR/WS frame/image) failures
        // AND main-frame load failures alike. The previous handler bounced
        // to setup.html unconditionally, so a transient sub-resource error
        // (e.g. a dropped WS frame on a heavy chat, a single 404 image
        // fetch) would reset the user back to the URL prompt. Symptom:
        // a rare URL setup screen appearing under load.
        //
        // The fix subclasses BridgeWebViewClient and overrides the
        // request-aware onReceivedError(View, Request, Error). We filter
        // to:
        //   1. request.isForMainFrame() — sub-resource fails are logged
        //      and ignored; useDashboardWs handles WS drops with
        //      auto-reconnect, and a 404 image isn't grounds for a reset.
        //   2. host matches the saved server URL — cross-origin transient
        //      failures (e.g. Authentik redirect blip) don't bounce.
        WebView wv = getBridge().getWebView();
        wv.setWebViewClient(new BridgeWebViewClient(getBridge()) {
            // Keep a main-frame navigation to a SIBLING of the active host's parent
            // domain (e.g. app.example.com -> auth.example.com) IN the WebView
            // instead of letting Capacitor punt it to the external browser. This is
            // the Authentik forward-auth redirect: it must complete in the WebView so
            // its cookie lands in the WebView's jar and it can bounce back to the
            // active host. The sibling host is deliberately NOT in allowNavigation, so
            // Capacitor's plugin bridge is never exposed to it (allowNavigation feeds
            // its allowedOriginRules), and the dashboard channel's rule names only
            // the active origin. Anything else falls through to Capacitor's default
            // (launchIntent -> external browser for non-allowed hosts).
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                if (request != null && request.isForMainFrame()) {
                    // The next document is not the one the setup gate saw loaded.
                    currentLoadedUrl = null;
                    if (isAuthSiblingHost(request.getUrl())) return false;
                }
                return super.shouldOverrideUrlLoading(view, request);
            }

            @Override
            public void onReceivedError(
                WebView view, WebResourceRequest request, WebResourceError error
            ) {
                super.onReceivedError(view, request, error);

                Uri reqUri = request.getUrl();
                String reqUrl = reqUri != null ? reqUri.toString() : "<null>";

                if (!request.isForMainFrame()) {
                    Log.w(
                        "OtoDock",
                        "Suppressed sub-resource error " + reqUrl
                            + " (code=" + error.getErrorCode()
                            + " desc=" + error.getDescription() + ")"
                    );
                    return;
                }

                InstallStore.Install active = installStore != null ? installStore.active() : null;
                if (active == null) return;
                String activeHost = activeHost();
                String failedHost = reqUri != null ? reqUri.getHost() : null;
                if (failedHost == null || !failedHost.equalsIgnoreCase(activeHost)) {
                    Log.w(
                        "OtoDock",
                        "Suppressed off-host main-frame error " + reqUrl
                            + " (activeHost=" + activeHost + ")"
                    );
                    return;
                }

                Log.e(
                    "OtoDock",
                    "Main-frame load failed for active server URL " + reqUrl
                        + " (code=" + error.getErrorCode()
                        + " desc=" + error.getDescription()
                        + ") — bouncing to setup page"
                );
                loadSetupPage("error=unreachable&url=" + Uri.encode(active.url));
            }
        });

        // Cold-start deep link: app was killed while browser was open for OAuth.
        // BridgeActivity.load() already replays the launch intent through
        // onNewIntent, which consumes an otodock:// link; this routes one it left.
        Intent launchIntent = getIntent();
        if (!sLaunchRoutingConsumed) {
            sLaunchRoutingConsumed = true;
            if (launchIntent != null) {
                if (Intent.ACTION_VIEW.equals(launchIntent.getAction())) {
                    Uri uri = launchIntent.getData();
                    if (uri != null && "otodock".equals(uri.getScheme())) {
                        // Route now; delivery is held until the SPA signals
                        // dashboardReady() (no fixed delay to race a slow boot / switch).
                        handleDeepLink(uri);
                    }
                }
                // Consume the deep-link data IN PLACE so a later recreate() (an install
                // switch) — which reuses this same intent object — can't re-process it.
                // (Only the otodock:// data, which is ours; notification extras like
                // google.message_id are left for Capacitor's own launch handling.)
                if (Intent.ACTION_VIEW.equals(launchIntent.getAction())) {
                    launchIntent.setData(null);
                }
            }
        }

        // Handle file downloads — WebView doesn't download files natively
        getBridge().getWebView().setDownloadListener((url, userAgent, contentDisposition, mimeType, contentLength) -> {
            // Guard: DownloadManager only supports http/https. blob:/data: URLs would
            // throw IllegalArgumentException and crash the app. For those, the JS side
            // is expected to bridge the data via Android.saveImageFromBase64() instead.
            if (url == null || !(url.startsWith("http://") || url.startsWith("https://"))) {
                Toast.makeText(this, "Cannot download this link", Toast.LENGTH_SHORT).show();
                return;
            }
            if (needsStorageAsk()) {
                pendingDownload = new String[]{url, userAgent, contentDisposition, mimeType};
                askForStorage();
                return;
            }
            startDownload(url, userAgent, contentDisposition, mimeType);
        });

        // Apply system bar + keyboard insets as padding on the WebView's parent
        // container: the one place that pads for the keyboard. Capacitor's
        // SystemBars plugin would pad the window's decor for it too (a band the
        // keyboard's height above the keyboard); capacitor.config.ts turns its
        // insets handling off, and load() hands that config to the plugins.
        View container = (View) getBridge().getWebView().getParent();
        // The boot field until the dashboard's theme arrives, so the deadline
        // path never shows white.
        containerColors[0] = getColor(R.color.bootField);
        containerColors[1] = getColor(R.color.bootFieldDeep);
        container.setBackground(new GradientDrawable(GradientDrawable.Orientation.TOP_BOTTOM, containerColors.clone()));
        getBridge().getWebView().setBackgroundColor(getColor(R.color.bootField));
        paintBars(getColor(R.color.bootField), getColor(R.color.bootFieldDeep), false);

        ViewCompat.setOnApplyWindowInsetsListener(container, (view, insets) -> {
            // Video fullscreen: go edge-to-edge — no system-bar padding so the
            // web <video> fullscreen fills the whole screen (system bars are
            // also hidden via the insets controller in enterVideoFullscreen).
            if (videoFullscreen) {
                view.setPadding(0, 0, 0, 0);
                return WindowInsetsCompat.CONSUMED;
            }
            Insets bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() | WindowInsetsCompat.Type.displayCutout()
            );
            Insets ime = insets.getInsets(WindowInsetsCompat.Type.ime());

            // When keyboard is open, use keyboard height for bottom; otherwise the
            // nav bar inset. The decorative 10dp that used to ride on top moved into
            // the dashboard CSS (.pb-composer-safe) so every client gets the gap.
            int bottomPadding = Math.max(ime.bottom, bars.bottom);

            view.setPadding(0, bars.top, 0, bottomPadding);
            // The screen's bars, for the page's boot screen: below Android 15
            // the window fits the navigation bar, so this view's own bottom
            // inset reads 0 there.
            WindowInsetsCompat root = ViewCompat.getRootWindowInsets(view);
            Insets screenBars = (root != null ? root : insets).getInsets(
                WindowInsetsCompat.Type.systemBars() | WindowInsetsCompat.Type.displayCutout()
            );
            pushBarInsets(getBridge().getWebView(), screenBars.top, screenBars.bottom, false);
            return WindowInsetsCompat.CONSUMED;
        });
    }

    /**
     * The system splash (Android 12 and later; the compat library's own view
     * before) is installed before the activity's content and held by a
     * pre-draw listener while bootHeld is true. Only a launch the system
     * shows a splash for is held: a new activity with no saved state, or the
     * first activity of a process the system ended in the background (saved
     * state, but nothing of this process has loaded yet). A recreate inside
     * the running process (an install switch) would hold a blank frame, so
     * it installs neither the condition nor the exit listener (below API 31
     * the listener alone adds a splash view that would flash on every
     * switch). The exit fades the splash onto the page's boot screen, which
     * paints the same field, and tells the page where the splash is so its
     * orbit and its own fade run at the right moments.
     */
    private void installBootSplash(Bundle savedInstanceState) {
        SplashScreen splash = SplashScreen.installSplashScreen(this);
        bootHeld = !sSwitchInProgress && (savedInstanceState == null || !sColdStarted);
        splashExpected = bootHeld;
        bootStart = SystemClock.uptimeMillis();
        if (!bootHeld) return;
        Log.i(TAG, "boot held");
        splash.setKeepOnScreenCondition(() -> {
            long held = SystemClock.uptimeMillis() - bootStart;
            if (bootHeld && pageCommitted && held >= BOOT_HOLD_MAX_MS) releaseBoot("deadline");
            else if (bootHeld && held >= BOOT_HOLD_CAP_MS) releaseBoot("deadline, no page");
            return bootHeld;
        });
        splash.setOnExitAnimationListener(provider -> {
            splashLeaving = true;
            View view = provider.getView();
            Runnable gone = () -> {
                if (splashRemoved) return;
                splashRemoved = true;
                provider.remove();
                // The library re-applies the theme's bar appearance on removal.
                reapplyBars();
                Log.i(TAG, "splash gone +" + (SystemClock.uptimeMillis() - bootStart) + "ms");
                tellSplashGone();
            };
            view.animate().alpha(0f).setDuration(BOOT_FADE_MS).withEndAction(gone).start();
            view.postDelayed(gone, BOOT_FADE_MS * 4);
            Log.i(TAG, "splash leaving +" + (SystemClock.uptimeMillis() - bootStart) + "ms");
            WebView wv = getBridge() != null ? getBridge().getWebView() : null;
            if (wv != null) tellSplash(wv, "leaving");
        });
    }

    /** Let the splash go: the condition is read on the next draw pass. */
    private void releaseBoot(String why) {
        if (!bootHeld) return;
        bootHeld = false;
        Log.i(TAG, "boot released (" + why + ") +" + (SystemClock.uptimeMillis() - bootStart) + "ms");
        runOnUiThread(() -> {
            View decor = getWindow().getDecorView();
            decor.postInvalidate();
            // A splash the system never hands over (the activity stopped
            // during the hold, a skin that keeps the view, a start the system
            // showed no splash for): the page must not wait for a cue that
            // never comes.
            decor.postDelayed(() -> {
                if (splashLeaving || splashGone) return;
                Log.i(TAG, "splash not handed over");
                tellSplashGone();
            }, BOOT_FADE_MS * 4);
            if (startupQuestionsWaiting) {
                startupQuestionsWaiting = false;
                askStartupQuestionsOnce();
            }
        });
    }

    /** The splash is no longer over the page: the active document learns so
     *  now, a later one at its first paint. */
    private void tellSplashGone() {
        splashGone = true;
        WebView wv = getBridge() != null ? getBridge().getWebView() : null;
        if (wv != null) tellSplash(wv, "gone");
    }

    /** The page's first real screen is painted (the channel): the splash
     *  leaves once the WebView's next frame carries that screen, or after
     *  a short wait when no frame comes while the splash holds the draw. */
    private void firstScreen() {
        if (!bootHeld) return;
        runOnUiThread(() -> {
            WebView wv = getBridge() != null ? getBridge().getWebView() : null;
            if (wv != null && WebViewFeature.isFeatureSupported(WebViewFeature.VISUAL_STATE_CALLBACK)) {
                WebViewCompat.postVisualStateCallback(wv, 0L, id -> releaseBoot("visual"));
                wv.postDelayed(() -> releaseBoot("cap"), BOOT_VISUAL_WAIT_MS);
            } else {
                releaseBoot("signal");
            }
        });
    }

    /** Where the system splash is, for the page's boot screen: "leaving" at
     *  the fade's start, "gone" once removed. */
    private void tellSplash(WebView wv, String state) {
        if (!showsDashboard(wv)) return;
        try {
            wv.evaluateJavascript("window.__otodockSplash && window.__otodockSplash('" + state + "')", null);
        } catch (Exception ignored) {}
    }

    /**
     * The bar heights, for the page's boot screen, which centres its mark on
     * the screen where the system splash draws its icon: the WebView's
     * viewport sits between the bars, so the page reads the two heights as
     * --oto-bar-top and --oto-bar-bottom (its px). Pushed when they change
     * and again when a document first paints.
     */
    private void pushBarInsets(WebView wv, int top, int bottom, boolean force) {
        if (top < 0 || bottom < 0 || wv == null) return;
        boolean changed = top != barTopPx || bottom != barBottomPx;
        barTopPx = top;
        barBottomPx = bottom;
        if ((!force && !changed) || !showsDashboard(wv)) return;
        float density = getResources().getDisplayMetrics().density;
        wv.evaluateJavascript(String.format(java.util.Locale.ROOT,
                "document.documentElement.style.setProperty('--oto-bar-top','%.1fpx');"
                        + "document.documentElement.style.setProperty('--oto-bar-bottom','%.1fpx')",
                top / density, bottom / density), null);
    }

    /** Whether the main frame shows the active install's dashboard: native's
     *  calls into the page go nowhere else (a sign-in's sibling host, the
     *  bundled page, a document still loading). */
    private boolean showsDashboard(WebView wv) {
        String url = wv.getUrl();
        return activeOrigin != null && url != null && NativeBridgePolicy.sameOrigin(activeOrigin, url);
    }

    /** The window's bar colours below Android 15 (later versions show what
     *  is drawn beneath, the container) and the icon appearance of both
     *  bars, recorded for reapplyBars. */
    @SuppressWarnings("deprecation")
    private void paintBars(int statusColor, int navigationColor, boolean lightBackground) {
        barStatusColor = statusColor;
        barNavigationColor = navigationColor;
        barLight = lightBackground;
        Window window = getWindow();
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.VANILLA_ICE_CREAM) {
            window.setStatusBarColor(statusColor);
            window.setNavigationBarColor(navigationColor);
        }
        WindowInsetsControllerCompat ctl = WindowCompat.getInsetsController(window, window.getDecorView());
        if (ctl != null) {
            ctl.setAppearanceLightStatusBars(lightBackground);
            ctl.setAppearanceLightNavigationBars(lightBackground);
        }
    }

    /** The bars as the page last set them: after the splash's removal and a
     *  configuration change, both of which re-apply the theme's own. */
    private void reapplyBars() {
        paintBars(barStatusColor, barNavigationColor, barLight);
    }

    /** The bundled setup page follows the phone's theme. */
    private void paintSetupPage() {
        boolean night = (getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK)
                == Configuration.UI_MODE_NIGHT_YES;
        setContainerColor(night ? getColor(R.color.pageDark) : getColor(R.color.pageLight));
    }

    private static final String FULL_SCREEN_OFFERED = "full_screen_offered";
    private static final String BATTERY_SETTINGS_OFFERED = "battery_settings_offered";
    // Asked once per process, at the first dashboard the person sees.
    private static boolean sStartupQuestionsAsked = false;

    /**
     * The two settings the alarm uses, each explained once and only after a
     * dashboard has loaded (never over the setup page or on a switch), one per
     * cold start, so no settings screen opens unprompted. Any answer records
     * the offer.
     */
    private void askStartupQuestionsOnce() {
        if (sStartupQuestionsAsked || isFinishing() || isDestroyed()) return;
        if (installStore == null || installStore.active() == null) return;
        // A dialog over a held window would end the starting window early.
        if (bootHeld) {
            startupQuestionsWaiting = true;
            return;
        }
        sStartupQuestionsAsked = true;
        if (offerFullScreenIntentOnce()) return;
        offerBatterySettingsOnce();
    }

    /**
     * Android 14+ gates USE_FULL_SCREEN_INTENT: Play leaves it on only for an
     * app it accepts as an alarm app, and the person can turn it off. Without
     * it the alarm rings but cannot light the screen over the lock screen.
     */
    private boolean offerFullScreenIntentOnce() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.UPSIDE_DOWN_CAKE) return false;
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm == null || nm.canUseFullScreenIntent()) return false;
        if (!offerOnce(FULL_SCREEN_OFFERED)) return false;
        return showSettingsOffer("Alarms on the lock screen",
                "To show a critical alarm over the lock screen, OtoDock needs the "
                        + "full-screen alarm access. Turn it on in the setting that opens.",
                "Open the setting",
                new Intent(Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT,
                        Uri.parse("package:" + getPackageName())));
    }

    /**
     * The alarm needs no battery exemption while the app is in regular use: its
     * high-priority FCM message starts the alarm's foreground service through
     * Doze. Android downgrades that message for an app it holds back (rarely
     * opened, or restricted), and then the alarm shows as a full-screen
     * notification that starts the sound once opened (NotificationService).
     * The direct exemption request needs a permission Play grants only when
     * the core function fails without it, so the person is pointed once to the
     * app's battery setting instead.
     */
    private boolean offerBatterySettingsOnce() {
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        ActivityManager am = (ActivityManager) getSystemService(ACTIVITY_SERVICE);
        boolean restricted = Build.VERSION.SDK_INT >= Build.VERSION_CODES.P
                && am != null && am.isBackgroundRestricted();
        if (pm == null || (pm.isIgnoringBatteryOptimizations(getPackageName()) && !restricted)) return false;
        if (!offerOnce(BATTERY_SETTINGS_OFFERED)) return false;
        return showSettingsOffer("Alarms on this phone",
                "Android can hold back alarms from an app you rarely open. To have "
                        + "OtoDock's alarms ring on time, open Battery in its settings and "
                        + "choose Unrestricted (on some phones: Battery optimization, then "
                        + "Don't optimize).",
                "Open OtoDock's settings",
                new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                        Uri.parse("package:" + getPackageName())));
    }

    /** True the first time for this key, recording it. */
    private boolean offerOnce(String key) {
        SharedPreferences prefs = getSharedPreferences(AUX_PREFS, MODE_PRIVATE);
        if (prefs.getBoolean(key, false)) return false;
        prefs.edit().putBoolean(key, true).apply();
        return true;
    }

    private boolean showSettingsOffer(String title, String message, String open, Intent settings) {
        try {
            new AlertDialog.Builder(this)
                    .setTitle(title)
                    .setMessage(message)
                    .setPositiveButton(open, (d, w) -> {
                        try {
                            startActivity(settings);
                        } catch (Exception e) {
                            // Some OEMs leave a settings page out.
                            try {
                                startActivity(new Intent(Settings.ACTION_SETTINGS));
                            } catch (Exception ignored) {}
                        }
                    })
                    .setNegativeButton("Not now", null)
                    .show();
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    private void createNotificationChannels() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;

        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm == null) return;

        // Delete old channels — Android won't update existing channel settings,
        // so we must delete and recreate to apply sound/importance changes.
        String[] oldIds = {"notif_info", "notif_success", "notif_warning", "notif_danger", "notif_ephemeral"};
        for (String id : oldIds) {
            nm.deleteNotificationChannel(id);
        }

        AudioAttributes audioAttr = new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_NOTIFICATION)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .build();

        AudioAttributes alarmAttr = new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_ALARM)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .build();

        // Info — heads-up, subtle chime, vibration. Respects silent mode.
        NotificationChannel info = new NotificationChannel(
                "notif_info", "Info", NotificationManager.IMPORTANCE_HIGH);
        info.setDescription("Routine notifications and reminders");
        info.setSound(
                Uri.parse("android.resource://" + getPackageName() + "/raw/notif_success"),
                audioAttr);
        info.enableVibration(true);
        info.setVibrationPattern(new long[]{0, 150});
        nm.createNotificationChannel(info);

        // Success — heads-up, chime, vibration. Respects silent mode.
        NotificationChannel success = new NotificationChannel(
                "notif_success", "Success", NotificationManager.IMPORTANCE_HIGH);
        success.setDescription("Task completions and positive updates");
        success.setSound(
                Uri.parse("android.resource://" + getPackageName() + "/raw/notif_success"),
                audioAttr);
        success.enableVibration(true);
        success.setVibrationPattern(new long[]{0, 200});
        nm.createNotificationChannel(success);

        // Warning — heads-up, warning sound, vibration. Respects silent mode.
        NotificationChannel warning = new NotificationChannel(
                "notif_warning", "Warning", NotificationManager.IMPORTANCE_HIGH);
        warning.setDescription("Attention-needed alerts");
        warning.setSound(
                Uri.parse("android.resource://" + getPackageName() + "/raw/notif_warning"),
                audioAttr);
        warning.enableVibration(true);
        warning.setVibrationPattern(new long[]{0, 300, 200, 300});
        nm.createNotificationChannel(warning);

        // Danger — alarm sound, max importance, overrides DND + silent mode.
        // Sound is played by AlarmService (looping), not by the channel itself.
        NotificationChannel danger = new NotificationChannel(
                "notif_danger", "Danger / Alarm", NotificationManager.IMPORTANCE_MAX);
        danger.setDescription("Critical alerts — overrides silent mode");
        danger.setSound(null, null); // AlarmService handles sound
        danger.enableVibration(true);
        danger.setVibrationPattern(new long[]{0, 500, 200, 500, 200, 500});
        danger.setBypassDnd(true);
        nm.createNotificationChannel(danger);

        // Ephemeral — turn-complete signal. IMPORTANCE_DEFAULT = sound + vibration
        // but NO heads-up banner. Entries stay in the shade until the app next
        // comes to the foreground (the TURN_COMPLETE_TAG sweep in onResume).
        NotificationChannel ephemeral = new NotificationChannel(
                "notif_ephemeral", "Turn Complete", NotificationManager.IMPORTANCE_DEFAULT);
        ephemeral.setDescription("Subtle alert when AI finishes responding");
        ephemeral.setSound(
                Uri.parse("android.resource://" + getPackageName() + "/raw/notif_turn_done"),
                audioAttr);
        ephemeral.enableVibration(true);
        ephemeral.setVibrationPattern(new long[]{0, 150, 120, 150});
        nm.createNotificationChannel(ephemeral);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        // If opened from alarm notification, stop the alarm service
        if (intent != null && intent.getBooleanExtra("dismiss_alarm", false)) {
            Intent stopIntent = new Intent(this, AlarmService.class);
            stopIntent.setAction(AlarmService.ACTION_DISMISS);
            startService(stopIntent);
        }
        // Deep link handling (OAuth callbacks via otodock:// scheme)
        if (intent != null && Intent.ACTION_VIEW.equals(intent.getAction())) {
            Uri uri = intent.getData();
            if (uri != null && "otodock".equals(uri.getScheme())) {
                handleDeepLink(uri);
                // Consumed here, whichever path delivered it: BridgeActivity.load()
                // replays the launch intent through onNewIntent on every recreate.
                intent.setData(null);
            }
        }
    }

    /**
     * Handle an otodock:// deep link from the system browser.
     *
     * - otodock://auth/callback?code=X&state=Y → navigate WebView to server's
     *   /auth/callback?code=X&state=Y (SPA AuthCallback component handles exchange).
     *   Used by the platform's own Authentik/OIDC login and the passkey hand-off.
     * - otodock://oauth/<provider>/complete?email=X → call window._handleDeepLink(url)
     *   in WebView (frontend JS handles the post-exchange UI refresh). Used by
     *   per-MCP OAuth flows (<provider> is the manifest's
     *   credentials.oauth.provider_id — google today; slack/linear/etc. later).
     *   The generic `oauth` host match below catches every provider without
     *   per-provider code here.
     *
     * Either is taken only while a sign-in of its kind that openAuthBrowser
     * started is in flight, and only for the install that started it (the
     * server's browser binding stays the primary control; this is the app's).
     */
    private void handleDeepLink(Uri uri) {
        InstallStore.Install target = deepLinkTarget(uri);
        if (target == null) {
            Log.w(TAG, "Dropped an otodock:// link: no sign-in in progress");
            return;
        }

        InstallStore.Install active = installStore.active();
        if (active == null || !target.id.equals(active.id)) {
            // Different install → switch to it and deliver once its dashboard is ready.
            setAuxString("pending_deeplink", uri.toString(), true);
            installStore.setActive(target.id);
            switchRecreate();
            return;
        }
        // Same install: deliver now if the page is ready, else stash for dashboardReady.
        if (dashboardIsReady) {
            deliverDeepLink(uri);
        } else {
            setAuxString("pending_deeplink", uri.toString(), true);
        }
    }

    /** Actually hand a resolved deep link to the (now active + ready) dashboard,
     *  re-checking the sign-in it ends: a link held across a switch or a slow
     *  boot is delivered only while that sign-in is still in flight. */
    private void deliverDeepLink(Uri uri) {
        WebView wv = getBridge().getWebView();
        InstallStore.Install active = installStore != null ? installStore.active() : null;
        InstallStore.Install target = deepLinkTarget(uri);
        if (wv == null || active == null || activeOrigin == null) return;
        if (target == null || !target.id.equals(active.id)) {
            Log.w(TAG, "Dropped an otodock:// link: no sign-in in progress");
            return;
        }

        String host = uri.getHost();
        String path = uri.getPath();
        if ("auth".equals(host) && "/callback".equals(path)) {
            // Authentik login: navigate the WebView to the server's callback page.
            String code = uri.getQueryParameter("code");
            String state = uri.getQueryParameter("state");
            if (code != null && state != null) {
                String callbackUrl = activeOrigin + "/auth/callback?code="
                        + Uri.encode(code) + "&state=" + Uri.encode(state);
                // The setup gate must not read the page this load replaces.
                currentLoadedUrl = null;
                runOnUiThread(() -> wv.loadUrl(callbackUrl));
            }
        } else if ("oauth".equals(host)) {
            // OAuth provider callback → frontend JS handler. JSON-encode the URL so a
            // crafted deep link can't break out of the JS string literal.
            String arg = JSONObject.quote(uri.toString());
            runOnUiThread(() -> wv.evaluateJavascript(
                    "window._handleDeepLink && window._handleDeepLink(" + arg + ")",
                    null));
        }
        // Flow consumed: clear it and the switch gate.
        clearAuthFlow();
        authInProgress = false;
    }

    /** The install a deep link goes to, or null to drop it (no sign-in of its
     *  kind in flight, or a tag naming another install). */
    private InstallStore.Install deepLinkTarget(Uri uri) {
        if (installStore == null || uri == null) return null;
        SharedPreferences p = getSharedPreferences(AUX_PREFS, MODE_PRIVATE);
        String tag = uri.getQueryParameter("install");
        InstallStore.Install tagged = tag != null ? installStore.byInstallId(tag) : null;
        String id = NativeBridgePolicy.deepLinkTarget(uri.getHost(),
                p.getString(AUTH_FLOW_KIND, null), p.getLong(AUTH_FLOW_UNTIL, 0L),
                SystemClock.elapsedRealtime(), p.getString(AUTH_FLOW_INSTALL, null),
                tagged != null ? tagged.id : null);
        return id != null ? installStore.byId(id) : null;
    }

    /** Arm the sign-in the system browser is about to run. elapsedRealtime
     *  survives a process kill; a reboot restarts it, which reads as expired. */
    private void armAuthFlow(String installId, String kind) {
        getSharedPreferences(AUX_PREFS, MODE_PRIVATE).edit()
                .putString(AUTH_FLOW_INSTALL, installId)
                .putString(AUTH_FLOW_KIND, kind)
                .putLong(AUTH_FLOW_UNTIL,
                        SystemClock.elapsedRealtime() + NativeBridgePolicy.AUTH_FLOW_TTL_MS)
                .commit();
    }

    private void clearAuthFlow() {
        getSharedPreferences(AUX_PREFS, MODE_PRIVATE).edit()
                .remove(AUTH_FLOW_INSTALL)
                .remove(AUTH_FLOW_KIND)
                .remove(AUTH_FLOW_UNTIL)
                .commit();
    }

    // --- File downloads ---
    // Below API 29 the public Downloads directory needs the storage permission:
    // a download or a base64 save waits here while it is asked for, and one
    // question is open at a time (a second request while the dialog is up
    // would be answered at once with an empty result). UI thread only.
    private static final int STORAGE_REQUEST = 1001;
    private String[] pendingDownload;
    private String[] pendingSave;
    private boolean storageAskOpen = false;

    private void askForStorage() {
        if (storageAskOpen) return;
        storageAskOpen = true;
        requestPermissions(new String[]{android.Manifest.permission.WRITE_EXTERNAL_STORAGE}, STORAGE_REQUEST);
    }

    private boolean needsStorageAsk() {
        return Build.VERSION.SDK_INT < Build.VERSION_CODES.Q
                && checkSelfPermission(android.Manifest.permission.WRITE_EXTERNAL_STORAGE)
                   != PackageManager.PERMISSION_GRANTED;
    }

    private void startDownload(String url, String userAgent, String contentDisposition, String mimeType) {
        String filename = null;
        try {
            Uri parsed = Uri.parse(url);
            filename = parsed.getQueryParameter("fn");
        } catch (Exception ignored) {}
        if (filename == null || filename.isEmpty()) {
            filename = URLUtil.guessFileName(url, contentDisposition, mimeType);
        }

        DownloadManager.Request request = new DownloadManager.Request(Uri.parse(url));
        request.setMimeType(mimeType);
        request.addRequestHeader("User-Agent", userAgent);

        String cookies = CookieManager.getInstance().getCookie(url);
        if (cookies != null) {
            request.addRequestHeader("cookie", cookies);
        }

        request.setTitle(filename);
        request.setDescription("Downloading...");
        request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
        request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, filename);

        DownloadManager dm = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
        if (dm != null) {
            dm.enqueue(request);
            Toast.makeText(this, "Downloading " + filename, Toast.LENGTH_SHORT).show();
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != STORAGE_REQUEST) return;
        storageAskOpen = false;
        String[] download = pendingDownload;
        String[] save = pendingSave;
        pendingDownload = null;
        pendingSave = null;
        // An empty result is a question that was interrupted: nothing was decided.
        if (grantResults.length == 0) {
            if (download != null || save != null) {
                Toast.makeText(this, "Saving to Downloads was interrupted. Try again.",
                        Toast.LENGTH_SHORT).show();
            }
            return;
        }
        if (grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            if (download != null) startDownload(download[0], download[1], download[2], download[3]);
            if (save != null) saveImageFromBase64(save[0], save[1], save[2]);
        } else if (download != null || save != null) {
            Toast.makeText(this, "Saving to Downloads needs the storage permission. "
                    + "Allow it for OtoDock in the phone's settings.", Toast.LENGTH_LONG).show();
        }
    }

    // --- Keep WebView/WebSocket alive ONLY while LLM is streaming ---
    private PowerManager.WakeLock wakeLock;
    private volatile boolean isStreaming = false;
    private boolean isPaused = false;
    // True while a web <video> is in fullscreen (set via the JS bridge) — the
    // inset listener then drops system-bar padding for true edge-to-edge.
    private volatile boolean videoFullscreen = false;
    private OrientationEventListener videoOrientationRelease;

    /**
     * The SENSOR_LANDSCAPE set on video-fullscreen entry is a CUE, not a pin:
     * once the user physically holds the phone in landscape (they accepted
     * the rotation), switch to FULL_SENSOR so turning the phone upright
     * rotates the video back to portrait fullscreen — with or without the
     * system auto-rotate toggle (YouTube-style). If they never rotate the
     * phone, the landscape cue stays.
     */
    private void armVideoOrientationRelease() {
        disarmVideoOrientationRelease();
        videoOrientationRelease = new OrientationEventListener(this) {
            @Override
            public void onOrientationChanged(int deg) {
                if (deg == ORIENTATION_UNKNOWN || !videoFullscreen) return;
                boolean physicallyLandscape =
                    (deg >= 60 && deg <= 120) || (deg >= 240 && deg <= 300);
                if (physicallyLandscape) {
                    setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_FULL_SENSOR);
                    disarmVideoOrientationRelease();
                }
            }
        };
        if (videoOrientationRelease.canDetectOrientation()) {
            videoOrientationRelease.enable();
        }
    }

    private void disarmVideoOrientationRelease() {
        if (videoOrientationRelease != null) {
            videoOrientationRelease.disable();
            videoOrientationRelease = null;
        }
    }

    // --- The dashboard channel ---

    /** Called by NativeChannelPlugin once the Bridge's WebView exists and before
     *  its first page load: the bridge surface is settled here for the life of
     *  this activity (a switch recreates it with the new install's origin). */
    void onBridgeWebViewReady(WebView wv) {
        // Capacitor's cookie plugin adds a raw interface whose setCookie writes any
        // cookie for any URL, from any frame. The plugin is off in this app
        // (native-bridge.js patches document.cookie only when it answers enabled).
        wv.removeJavascriptInterface("CapacitorCookiesAndroidInterface");
        if (activeOrigin != null) {
            try {
                WebViewCompat.addWebMessageListener(wv, NativeBridgePolicy.CHANNEL,
                        Collections.singleton(activeOrigin), this::onNativeMessage);
            } catch (RuntimeException e) {
                // load() checked the feature and the origin; with no channel every
                // dashboard wrapper is a no-op.
                Log.e(TAG, "Native channel not registered for " + activeOrigin, e);
            }
        }
        wv.addJavascriptInterface(new SetupBridge(), "Android");
    }

    /** The channel's listener (UI thread): judges the sender, then queues the call. */
    private void onNativeMessage(WebView view, WebMessageCompat message, Uri sourceOrigin,
                                 boolean isMainFrame, JavaScriptReplyProxy reply) {
        if (!isMainFrame) {
            refuseNative("a child frame");
            return;
        }
        if (sourceOrigin == null
                || !NativeBridgePolicy.sameOrigin(activeOrigin, sourceOrigin.toString())) {
            refuseNative("another origin");
            return;
        }
        if (message == null || message.getType() != WebMessageCompat.TYPE_STRING) {
            refuseNative("a non-text message");
            return;
        }
        WebMessagePortCompat[] ports = message.getPorts();
        String data = message.getData();
        if ((ports != null && ports.length > 0) || data == null
                || data.length() > NativeBridgePolicy.MAX_MESSAGE_CHARS) {
            refuseNative("a malformed or oversized message");
            return;
        }
        if (!NativeBridgePolicy.isDashboardPage(view.getUrl())) {
            refuseNative("a page outside the dashboard");
            return;
        }
        NATIVE_CALLS.execute(() -> dispatchNative(data, reply));
    }

    private void refuseNative(String why) {
        Log.w(TAG, "Native channel refused a message from " + why);
    }

    /** Runs one channel call, on NATIVE_CALLS. */
    private void dispatchNative(String data, JavaScriptReplyProxy reply) {
        if (isFinishing() || isDestroyed()) return;
        // A deep-link switch moves the active pointer before its recreate: a call
        // arriving in between belongs to the page being replaced.
        InstallStore.Install active = installStore.active();
        if (active == null || !NativeBridgePolicy.sameOrigin(activeOrigin, active.url)) return;
        String[] parts = NativeBridgePolicy.splitMessage(data);
        final JSONObject head;
        try {
            head = new JSONObject(parts[0]);
        } catch (Exception e) {
            refuseNative("an unreadable message");
            return;
        }
        String method = head.optString("m", "");
        JSONArray args = head.optJSONArray("a");
        if (args == null) args = new JSONArray();
        int id = head.optInt("id", 0);
        try {
            switch (method) {
                case "setStreaming": setStreaming(args.optBoolean(0)); break;
                case "setContainerColor": setContainerColor(arg(args, 0)); break;
                case "enterVideoFullscreen": enterVideoFullscreen(args.optBoolean(0)); break;
                case "exitVideoFullscreen": exitVideoFullscreen(); break;
                case "flushCookies": CookieManager.getInstance().flush(); break;
                case "dashboardReady": dashboardReady(); break;
                case "firstScreen": firstScreen(); break;
                case "setAuthInProgress": setAuthInProgress(args.optBoolean(0)); break;
                case "setSwitchBusy": switchBusyFlag = args.optBoolean(0); break;
                case "openAuthBrowser": openAuthBrowser(arg(args, 0), arg(args, 1), reply, id); break;
                case "saveImageFromBase64": saveImageFromBase64(parts[1], arg(args, 0), arg(args, 1)); break;
                case "getInstallations":
                    replyNative(reply, id, new JSONArray(installStore.toPublicJson()));
                    break;
                case "setFavorite": installStore.setFavorite(arg(args, 0)); break;
                case "renameInstallation": installStore.rename(arg(args, 0), arg(args, 1)); break;
                case "switchInstallation": switchInstallation(arg(args, 0)); break;
                case "switchToInstall":
                    replyNative(reply, id, switchToInstall(arg(args, 0), arg(args, 1)));
                    break;
                case "openAddInstallation": loadSetupPage("mode=add"); break;
                case "openPrivacyPolicy": openPrivacyPolicyInBrowser(); break;
                case "removeInstallation": removeInstallation(arg(args, 0)); break;
                case "disconnectServer": disconnectServer(); break;
                case "foldNote": {
                    FoldDiagnostics diag = foldDiag;
                    if (diag != null) diag.page(arg(args, 0));
                    break;
                }
                default: Log.w(TAG, "Native channel: unknown method " + method);
            }
        } catch (Exception e) {
            Log.w(TAG, "Native channel: " + method + " failed", e);
        }
    }

    private static String arg(JSONArray args, int i) {
        return args.isNull(i) ? null : args.optString(i, null);
    }

    /** Answer a replying call as {"id": n, "r": value}, on the UI thread. */
    private void replyNative(JavaScriptReplyProxy reply, int id, Object value) {
        if (reply == null || id <= 0) return;
        final String json;
        try {
            json = new JSONObject().put("id", id)
                    .put("r", value == null ? JSONObject.NULL : value).toString();
        } catch (Exception e) {
            return;
        }
        runOnUiThread(() -> {
            try {
                reply.postMessage(json);
            } catch (Exception ignored) {}
        });
    }

    private void setStreaming(boolean streaming) {
        isStreaming = streaming;
        if (streaming && isPaused) {
            acquireWakeLock();
            resumeWebView();
        } else if (!streaming) {
            releaseWakeLock();
        }
    }

    private void setContainerColor(String hexColor) {
        try {
            setContainerColor(Color.parseColor(hexColor));
        } catch (Exception e) {
            Log.w(TAG, "setContainerColor: not a colour");
        }
    }

    /** The theme's page colour behind the bars (the container, cross-faded
     *  from whatever it shows, the boot field included), the WebView's own
     *  background (what shows between two documents) and the bars. */
    private void setContainerColor(int to) {
        // The field is what the app paints before any page; a page sends it
        // for apps older than the boot screen (boot.js), nothing to do here.
        if (to == getColor(R.color.bootField)) return;
        runOnUiThread(() -> {
            try {
                WebView wv = getBridge().getWebView();
                View container = (View) wv.getParent();
                wv.setBackgroundColor(to);
                paintBars(to, to, ColorUtils.calculateLuminance(to) > 0.5);
                if (containerFade != null) containerFade.cancel();
                final int fromTop = containerColors[0];
                final int fromBottom = containerColors[1];
                final ArgbEvaluator evaluator = new ArgbEvaluator();
                final GradientDrawable field = new GradientDrawable(
                        GradientDrawable.Orientation.TOP_BOTTOM, containerColors.clone());
                container.setBackground(field);
                containerFade = ValueAnimator.ofFloat(0f, 1f);
                containerFade.setDuration(CONTAINER_FADE_MS);
                containerFade.addUpdateListener(a -> {
                    float f = (Float) a.getAnimatedValue();
                    containerColors[0] = (Integer) evaluator.evaluate(f, fromTop, to);
                    containerColors[1] = (Integer) evaluator.evaluate(f, fromBottom, to);
                    field.setColors(containerColors.clone());
                });
                containerFade.start();
            } catch (Exception ignored) {}
        });
    }

    /**
     * Enter edge-to-edge video fullscreen: drop the WebView's system-bar
     * padding (so a web &lt;video&gt; fullscreen fills the screen), hide
     * the status/nav bars (immersive), and lock landscape for wide
     * videos. Called by VideoPlayer.tsx on fullscreen enter.
     */
    private void enterVideoFullscreen(boolean landscape) {
        runOnUiThread(() -> {
            videoFullscreen = true;
            try {
                View container = (View) getBridge().getWebView().getParent();
                ViewCompat.requestApplyInsets(container);
                WindowInsetsControllerCompat ctl = WindowCompat.getInsetsController(
                    getWindow(), getWindow().getDecorView());
                if (ctl != null) {
                    ctl.setSystemBarsBehavior(
                        WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
                    ctl.hide(WindowInsetsCompat.Type.systemBars());
                }
                if (landscape) {
                    setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE);
                    armVideoOrientationRelease();
                }
            } catch (Exception ignored) {}
        });
    }

    /** Exit video fullscreen: restore padding, system bars, orientation. */
    private void exitVideoFullscreen() {
        runOnUiThread(() -> {
            videoFullscreen = false;
            disarmVideoOrientationRelease();
            try {
                View container = (View) getBridge().getWebView().getParent();
                ViewCompat.requestApplyInsets(container);
                WindowInsetsControllerCompat ctl = WindowCompat.getInsetsController(
                    getWindow(), getWindow().getDecorView());
                if (ctl != null) {
                    ctl.show(WindowInsetsCompat.Type.systemBars());
                }
                setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED);
            } catch (Exception ignored) {}
        });
    }

    /** The SPA signals it has mounted; deliver any deep link held for it. */
    private void dashboardReady() {
        dashboardIsReady = true;
        runOnUiThread(this::askStartupQuestionsOnce);
        String pending = takeAuxString("pending_deeplink");
        if (pending != null) {
            try { deliverDeepLink(Uri.parse(pending)); } catch (Exception ignored) {}
        }
    }

    /** Mark an auth flow active/inactive (Custom Tab + device-code, which
     *  don't go through openAuthBrowser) so a switch is blocked mid-flow. */
    private void setAuthInProgress(boolean active) {
        if (active) beginAuth();
        else authInProgress = false;
    }

    /** Switch the active install (rebuilds the Bridge for the new server). */
    private void switchInstallation(String id) {
        if (id == null || authActive()) return;  // don't switch mid-auth-flow
        if (installStore.byId(id) == null) return;
        if (id.equals(installStore.activeIdValue())) return;
        if (switchIsBusy()) {
            runOnUiThread(() -> confirmNative(
                    "Switch installation",
                    "You have an active session here that will end. Switch anyway?",
                    () -> captureRouteThenSwitch(id)));
        } else {
            captureRouteThenSwitch(id);
        }
    }

    /**
     * Route a notification tap to the install it belongs to. The dashboard's
     * push handler calls this with the push's source install_id; native owns
     * the decision (it holds the install_id → install binding). Returns true iff
     * it actually switched (the source is a known, different install), so the
     * dashboard knows whether to navigate in place instead. The click_url is
     * sanitized and used as the target's boot route.
     */
    private boolean switchToInstall(String installId, String clickUrl) {
        InstallStore.Install target = installStore.byInstallId(installId);
        InstallStore.Install active = installStore.active();
        if (target == null || active == null || target.id.equals(active.id)) return false;
        installStore.setLastRoute(target.id, sanitizeClickUrl(clickUrl));
        installStore.setActive(target.id);
        switchRecreate();
        return true;
    }

    /** Remove an install (native-confirmed: a JS confirm is bypassable). */
    private void removeInstallation(String id) {
        if (installStore.byId(id) == null) return;
        runOnUiThread(() -> confirmNative(
                "Remove installation",
                "Remove this server from the app? You'll need to add it again to reconnect.",
                () -> doRemoveInstall(id)));
    }

    /** Disconnect = remove the active install (native-confirmed). */
    private void disconnectServer() {
        String id = installStore.activeIdValue();
        if (id == null) return;
        runOnUiThread(() -> confirmNative(
                "Disconnect",
                "Disconnect from this server? You'll need to add it again to reconnect.",
                () -> doRemoveInstall(id)));
    }

    /**
     * Open an https address in the system browser for a sign-in ({@code kind}
     * "auth") or a per-MCP / account connect ("oauth"). Another site than the
     * active install is named in a native confirm first (the passkey page is on
     * the active install and opens directly). Replies true when the browser
     * opened, false when refused or declined; the dialog never holds the
     * channel's thread.
     */
    private void openAuthBrowser(String url, String kind, JavaScriptReplyProxy reply, int id) {
        String target = NativeBridgePolicy.authBrowserUrl(url);
        InstallStore.Install active = installStore.active();
        if (target == null || !NativeBridgePolicy.isFlowKind(kind) || active == null) {
            replyNative(reply, id, false);
            return;
        }
        String installId = active.id;
        if (!NativeBridgePolicy.needsHostConfirm(target, activeOrigin)) {
            launchAuthBrowser(target, kind, installId, reply, id);
            return;
        }
        String host = Uri.parse(target).getHost();
        runOnUiThread(() -> confirmNative(
                "Open in your browser",
                "Open " + host + " in your browser to continue?",
                confirmed -> {
                    if (confirmed) {
                        NATIVE_CALLS.execute(() -> launchAuthBrowser(target, kind, installId, reply, id));
                    } else {
                        replyNative(reply, id, false);
                    }
                }));
    }

    /** Persist the WebView's cookies (a sign-in start has just set its binding
     *  cookie), arm the sign-in the deep link will be checked against, then start
     *  the browser. Runs on NATIVE_CALLS. */
    private void launchAuthBrowser(String target, String kind, String installId,
                                   JavaScriptReplyProxy reply, int id) {
        if (isFinishing() || isDestroyed()) {
            replyNative(reply, id, false);
            return;
        }
        CookieManager.getInstance().flush();
        armAuthFlow(installId, kind);
        beginAuth();
        runOnUiThread(() -> {
            try {
                Intent browserIntent = new Intent(Intent.ACTION_VIEW, Uri.parse(target));
                browserIntent.addCategory(Intent.CATEGORY_BROWSABLE);
                startActivity(browserIntent);
                replyNative(reply, id, true);
            } catch (Exception e) {
                clearAuthFlow();
                authInProgress = false;
                replyNative(reply, id, false);
            }
        });
    }

    private long privacyPolicyOpenedAt = 0L;

    /** The privacy policy in the system browser (the setup page's link and App
     *  Settings'). Any thread. At most once in two seconds, so a page cannot
     *  keep pushing the browser to the front. */
    private void openPrivacyPolicyInBrowser() {
        runOnUiThread(() -> {
            long now = SystemClock.elapsedRealtime();
            if (privacyPolicyOpenedAt != 0L && now - privacyPolicyOpenedAt < 2000L) return;
            privacyPolicyOpenedAt = now;
            try {
                Intent browser = new Intent(Intent.ACTION_VIEW, Uri.parse(NativeBridgePolicy.PRIVACY_POLICY_URL));
                browser.addCategory(Intent.CATEGORY_BROWSABLE);
                startActivity(browser);
            } catch (Exception e) {
                Toast.makeText(this, "No browser to open " + NativeBridgePolicy.PRIVACY_POLICY_URL,
                        Toast.LENGTH_LONG).show();
            }
        });
    }

    /**
     * Save a base64 payload to the system Downloads folder. Called by the
     * lightbox (ImageLightbox.tsx) and the wake diagnostics (wakeDiag.ts) inside
     * the app. A blob: URL cannot be downloaded by the native DownloadManager, so
     * the JS side passes the bytes through the channel instead. Only the types
     * those two save are written, under a name whose extension comes from the
     * type, and at most NativeBridgePolicy.MAX_SAVE_BYTES.
     *
     * On Android Q+ (API 29+) the file is inserted via MediaStore (no storage
     * permission needed). On older devices it is written to the public Downloads
     * directory and announced to the media scanner, once the storage permission
     * is granted (the save waits for the answer, see askForStorage).
     */
    private void saveImageFromBase64(String base64Data, String filename, String mimeType) {
        String extension = NativeBridgePolicy.saveExtension(mimeType);
        if (extension == null || base64Data == null
                || !NativeBridgePolicy.saveBase64Fits(base64Data.length())) {
            runOnUiThread(() -> Toast.makeText(MainActivity.this,
                    "This file can't be saved", Toast.LENGTH_SHORT).show());
            return;
        }
        if (needsStorageAsk()) {
            // A second save while the question is open replaces this one.
            runOnUiThread(() -> {
                pendingSave = new String[]{base64Data, filename, mimeType};
                askForStorage();
            });
            return;
        }
        final String safeMime = NativeBridgePolicy.normalizeMime(mimeType);
        final String safeName = NativeBridgePolicy.saveFileName(filename, extension);

        new Thread(() -> {
            // Strip optional "data:image/jpeg;base64," prefix if present.
            String payload = base64Data;
            int comma = payload.indexOf(',');
            if (payload.startsWith("data:") && comma > 0) {
                payload = payload.substring(comma + 1);
            }
            final byte[] bytes;
            try {
                bytes = Base64.decode(payload, Base64.DEFAULT);
            } catch (IllegalArgumentException e) {
                runOnUiThread(() -> Toast.makeText(MainActivity.this,
                        "Download failed: invalid image data", Toast.LENGTH_SHORT).show());
                return;
            }
            if (bytes.length > NativeBridgePolicy.MAX_SAVE_BYTES) {
                runOnUiThread(() -> Toast.makeText(MainActivity.this,
                        "This file can't be saved", Toast.LENGTH_SHORT).show());
                return;
            }
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    ContentValues values = new ContentValues();
                    values.put(MediaStore.Downloads.DISPLAY_NAME, safeName);
                    values.put(MediaStore.Downloads.MIME_TYPE, safeMime);
                    values.put(MediaStore.Downloads.RELATIVE_PATH,
                            Environment.DIRECTORY_DOWNLOADS);
                    values.put(MediaStore.Downloads.IS_PENDING, 1);

                    Uri target = getContentResolver().insert(
                            MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
                    if (target == null) throw new java.io.IOException("insert failed");
                    try (java.io.OutputStream os =
                                 getContentResolver().openOutputStream(target)) {
                        if (os == null) throw new java.io.IOException("openOutputStream failed");
                        os.write(bytes);
                    }
                    values.clear();
                    values.put(MediaStore.Downloads.IS_PENDING, 0);
                    getContentResolver().update(target, values, null, null);
                } else {
                    java.io.File dir = Environment.getExternalStoragePublicDirectory(
                            Environment.DIRECTORY_DOWNLOADS);
                    if (!dir.exists()) dir.mkdirs();
                    java.io.File out = new java.io.File(dir, safeName);
                    try (java.io.FileOutputStream fos = new java.io.FileOutputStream(out)) {
                        fos.write(bytes);
                    }
                    MediaScannerConnection.scanFile(MainActivity.this,
                            new String[]{out.getAbsolutePath()},
                            new String[]{safeMime}, null);
                }
                runOnUiThread(() -> Toast.makeText(MainActivity.this,
                        "Saved " + safeName + " to Downloads", Toast.LENGTH_SHORT).show());
            } catch (Exception e) {
                runOnUiThread(() -> Toast.makeText(MainActivity.this,
                        "Download failed: " + e.getMessage(), Toast.LENGTH_SHORT).show());
            }
        }).start();
    }

    // --- The setup page's raw interface ---

    /** The raw interface the bundled setup page calls synchronously. A raw
     *  interface is present in every frame and cannot be scoped, so every method
     *  refuses unless the bundled page is the top document (callerIsSetup). */
    private final class SetupBridge {

        /** Full list (id/url/label/favorite/active) for the "unreachable" picker. */
        @JavascriptInterface
        public String getInstallations() {
            if (!callerIsSetup()) return "[]";
            return installStore.toPublicJson();
        }

        /** Re-favorite a reachable install from the "unreachable" picker. */
        @JavascriptInterface
        public void setFavorite(String id) {
            if (!callerIsSetup()) return;
            installStore.setFavorite(id);
        }

        /** The privacy policy in the system browser (the page's link). */
        @JavascriptInterface
        public void openPrivacyPolicy() {
            if (!callerIsSetup()) return;
            openPrivacyPolicyInBrowser();
        }

        /** Cancel the add-server screen and return to the active install. */
        @JavascriptInterface
        public void cancelAddInstallation() {
            if (!callerIsSetup()) return;
            if (installStore.active() == null) return;
            switchRecreate();
        }

        /** Switch to a chosen install from the setup / "server unreachable" screen.
         *  Unlike switchInstallation it does NOT capture the current route (the
         *  current page is setup.html, not a real dashboard route) and skips the
         *  busy/auth guards (no live session on the setup screen). Lets the user
         *  recover when the favorite is unreachable. */
        @JavascriptInterface
        public void switchFromSetup(String id) {
            if (!callerIsSetup()) return;
            if (installStore.byId(id) == null) return;
            installStore.setActive(id);
            switchRecreate();
        }

        /**
         * Validate a server URL from Java (bypasses CORS + forward auth), then
         * add it and switch to it. Reports window._onValidationResult(type, msg)
         * on error.
         */
        @JavascriptInterface
        public void addInstallation(String rawUrl) {
            if (!callerIsSetup()) return;
            String url = NativeBridgePolicy.serverUrl(rawUrl);
            if (url == null) {
                postValidationCallback("error",
                        "Invalid URL. HTTPS is required (plain http only for local network addresses).");
                return;
            }
            new Thread(() -> {
                try {
                    java.net.HttpURLConnection conn = (java.net.HttpURLConnection)
                            new java.net.URL(url + "/health").openConnection();
                    conn.setConnectTimeout(8000);
                    conn.setReadTimeout(8000);
                    conn.setInstanceFollowRedirects(false);
                    conn.setRequestMethod("GET");
                    int code = conn.getResponseCode();
                    conn.disconnect();

                    // Any HTTP response (200, 302 auth redirect, etc.) means
                    // the server is reachable: add it and switch to it.
                    if (code > 0) {
                        commitInstall(url);
                    } else {
                        postValidationCallback("error", "Could not reach server.");
                    }
                } catch (Exception e) {
                    postValidationCallback("error",
                            "Could not reach server. Check the URL and try again.");
                }
            }).start();
        }
    }

    private void setupBackgroundKeepAlive() {
        // Lifecycle observer to detect pause/resume (BridgeActivity marks onPause/onResume as final)
        getLifecycle().addObserver(new DefaultLifecycleObserver() {
            @Override
            public void onPause(LifecycleOwner owner) {
                isPaused = true;
                if (isStreaming) {
                    acquireWakeLock();
                    getBridge().getWebView().postDelayed(() -> resumeWebView(), 100);
                }
            }

            @Override
            public void onResume(LifecycleOwner owner) {
                isPaused = false;
                releaseWakeLock();
                clearTurnCompleteNotifications();
                // Bridge a force-health-check signal into the WebView.
                // visibilitychange can be unreliable on short screen off/on
                // cycles where the WebView never enters the hidden state; the
                // JS hook listens for this event and runs a ping/pong to
                // confirm the WS is actually alive.
                WebView wv = getBridge().getWebView();
                if (wv != null) {
                    wv.evaluateJavascript(
                        "window.dispatchEvent(new Event('otodock:force-health-check'));",
                        null);
                }
            }
        });
    }

    /**
     * Sweep every piled-up end-of-turn notification when the app comes to the
     * foreground — the user is looking at the dashboard now, so the "response
     * ready" entries have served their purpose. Only entries posted under
     * NotificationService.TURN_COMPLETE_TAG are cleared (from any install);
     * severity notifications and the alarm stay untouched.
     */
    private void clearTurnCompleteNotifications() {
        try {
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm == null) return;
            for (android.service.notification.StatusBarNotification sbn : nm.getActiveNotifications()) {
                if (NotificationService.TURN_COMPLETE_TAG.equals(sbn.getTag())) {
                    nm.cancel(sbn.getTag(), sbn.getId());
                }
            }
        } catch (Exception ignored) {}
    }

    /** Add (or reuse) an install for this URL, make it active, and switch to it.
     *  Cookies are NOT cleared — each install keeps its own host-scoped session. */
    private void commitInstall(String url) {
        InstallStore.Install target = installStore.add(url, extractHost(url));
        installStore.setActive(target.id);
        switchRecreate();
    }

    /** Mark a switch in progress and rebuild the Bridge for the active install. */
    private void switchRecreate() {
        sSwitchInProgress = true;
        runOnUiThread(() -> {
            InstallStore.Install a = installStore != null ? installStore.active() : null;
            if (a != null) {
                Toast.makeText(this, "Switching to " + a.label, Toast.LENGTH_SHORT).show();
            }
            recreate();
        });
    }

    /** A switch would lose live work (an LLM stream or a dashboard-reported session). */
    private boolean switchIsBusy() {
        return isStreaming || switchBusyFlag;
    }

    /** Capture the current install's in-app route (so it reopens there) and switch. */
    private void captureRouteThenSwitch(String targetId) {
        runOnUiThread(() -> {
            WebView wv = getBridge().getWebView();
            String curActive = installStore.activeIdValue();
            if (wv == null || curActive == null) {
                installStore.setActive(targetId);
                switchRecreate();
                return;
            }
            wv.evaluateJavascript("(location.pathname + location.search)", value -> {
                String route = unquoteJs(value);
                if (route != null && isSafeRoutePath(route) && !route.contains("setup")) {
                    installStore.setLastRoute(curActive, route);
                }
                installStore.setActive(targetId);
                switchRecreate();
            });
        });
    }

    /** Decode evaluateJavascript's JSON-encoded string result; null if not a string. */
    private String unquoteJs(String v) {
        if (v == null) return null;
        try {
            Object o = new org.json.JSONTokener(v).nextValue();
            return (o instanceof String) ? (String) o : null;
        } catch (Exception e) {
            return null;
        }
    }

    /** Switch to the next/previous install in list order (wraps). dir = +1/-1. */
    private void cycleInstall(int dir) {
        if (installStore == null || videoFullscreen || authActive() || switchIsBusy()) {
            if (switchIsBusy()) {
                Toast.makeText(this, "Finish the current activity before switching", Toast.LENGTH_SHORT).show();
            }
            return;
        }
        java.util.List<InstallStore.Install> list = installStore.list();
        if (list.size() < 2) return;
        String activeId = installStore.activeIdValue();
        int idx = -1;
        for (int i = 0; i < list.size(); i++) {
            if (list.get(i).id.equals(activeId)) { idx = i; break; }
        }
        if (idx < 0) return;
        InstallStore.Install target = list.get((idx + dir + list.size()) % list.size());
        if (target.id.equals(activeId)) return;
        captureRouteThenSwitch(target.id);
    }

    // --- 3-finger horizontal swipe → switch installation ---

    @Override
    public boolean dispatchTouchEvent(MotionEvent ev) {
        trackThreeFingerSwipe(ev);
        return super.dispatchTouchEvent(ev);  // never consume — let the WebView scroll
    }

    private void trackThreeFingerSwipe(MotionEvent ev) {
        int a = ev.getActionMasked();
        if (ev.getPointerCount() >= 3
                && (a == MotionEvent.ACTION_POINTER_DOWN || a == MotionEvent.ACTION_MOVE)) {
            float cx = avgX(ev), cy = avgY(ev);
            if (!swipeArmed) { swipeArmed = true; swipeStartX = cx; swipeStartY = cy; }
            swipeLastX = cx; swipeLastY = cy;
        } else if (swipeArmed
                && (a == MotionEvent.ACTION_POINTER_UP || a == MotionEvent.ACTION_UP
                    || a == MotionEvent.ACTION_CANCEL)) {
            boolean fire = a != MotionEvent.ACTION_CANCEL;
            swipeArmed = false;
            if (fire) {
                float dx = swipeLastX - swipeStartX, dy = swipeLastY - swipeStartY;
                float threshold = 0.18f * getResources().getDisplayMetrics().widthPixels;
                if (Math.abs(dx) > threshold && Math.abs(dx) > 1.5f * Math.abs(dy)) {
                    final int dir = dx < 0 ? 1 : -1;  // swipe left → next install
                    runOnUiThread(() -> cycleInstall(dir));
                }
            }
        }
    }

    private float avgX(MotionEvent ev) {
        float s = 0; int n = ev.getPointerCount();
        for (int i = 0; i < n; i++) s += ev.getX(i);
        return n > 0 ? s / n : 0;
    }

    private float avgY(MotionEvent ev) {
        float s = 0; int n = ev.getPointerCount();
        for (int i = 0; i < n; i++) s += ev.getY(i);
        return n > 0 ? s / n : 0;
    }

    /** Remove an install: clear the removed host's cookies (best-effort), or all
     *  cookies when none remain (full reset). */
    private void doRemoveInstall(String id) {
        InstallStore.Install removed = installStore.byId(id);
        String removedUrl = removed != null ? removed.url : null;
        installStore.remove(id);
        if (installStore.active() == null) {
            CookieManager.getInstance().removeAllCookies(null);
            CookieManager.getInstance().flush();
        } else if (removedUrl != null) {
            clearHostCookies(removedUrl);
        }
        switchRecreate();
    }

    /** Best-effort expire the cookies for a host (path-scoped / parent-domain
     *  cookies may survive; the only guaranteed wipe is removeAllCookies). */
    private void clearHostCookies(String url) {
        try {
            CookieManager cm = CookieManager.getInstance();
            String cookies = cm.getCookie(url);
            if (cookies == null) return;
            for (String pair : cookies.split(";")) {
                int eq = pair.indexOf('=');
                String name = (eq > 0 ? pair.substring(0, eq) : pair).trim();
                if (!name.isEmpty()) {
                    // A __Host- / __Secure- cookie (the login cookies on an https
                    // install) is accepted, and so expired, only with Secure.
                    boolean prefixed = name.startsWith("__Host-") || name.startsWith("__Secure-");
                    cm.setCookie(url, name + "=; Max-Age=0; Path=/" + (prefixed ? "; Secure" : ""));
                }
            }
            cm.flush();
        } catch (Exception ignored) {}
    }

    /** True when the WebView's current page is the local (bundled) setup screen. */
    private boolean callerIsSetup() {
        String cur = currentLoadedUrl;
        // Must be the BUNDLED asset (file://android_asset), not merely any page
        // whose URL contains "setup.html" — otherwise a compromised active-install
        // server could serve its own /setup.html to reach setup-only bridge methods
        // (addInstallation / switchFromSetup / cancelAddInstallation). setup.html is
        // always loaded from file:///android_asset/public/setup.html.
        return cur != null && cur.startsWith("file:///android_asset/") && cur.contains("setup.html");
    }

    /** Arm the auth gate with a safety deadline (so a never-returning browser flow
     *  can't lock switching forever). */
    private void beginAuth() {
        authInProgress = true;
        authInProgressUntil = android.os.SystemClock.elapsedRealtime() + 5 * 60 * 1000L;
    }

    /** True while an auth flow is genuinely in progress (within the deadline). */
    private boolean authActive() {
        return authInProgress && android.os.SystemClock.elapsedRealtime() < authInProgressUntil;
    }

    /** Show a native confirm dialog; run onYes on confirmation. UI thread only. */
    private void confirmNative(String title, String message, Runnable onYes) {
        confirmNative(title, message, confirmed -> {
            if (confirmed) onYes.run();
        });
    }

    private interface ConfirmResult {
        void done(boolean confirmed);
    }

    /** Show a native confirm dialog and report the answer exactly once: Cancel,
     *  the back key, a tap outside and a dialog that cannot show all answer no.
     *  UI thread only. */
    private void confirmNative(String title, String message, ConfirmResult result) {
        AtomicBoolean answered = new AtomicBoolean(false);
        ConfirmResult once = confirmed -> {
            if (answered.compareAndSet(false, true)) result.done(confirmed);
        };
        if (isFinishing() || isDestroyed()) {
            once.done(false);
            return;
        }
        try {
            new AlertDialog.Builder(this)
                    .setTitle(title)
                    .setMessage(message)
                    .setNegativeButton("Cancel", (d, w) -> once.done(false))
                    .setPositiveButton("Confirm", (d, w) -> once.done(true))
                    .setOnCancelListener(d -> once.done(false))
                    .setOnDismissListener(d -> once.done(false))
                    .show();
        } catch (Exception e) {
            once.done(false);
        }
    }

    /** Load the bundled setup page; a deep link held meanwhile waits for the
     *  next dashboard's dashboardReady. */
    private void loadSetupPage(String query) {
        dashboardIsReady = false;
        WebView wv = getBridge() != null ? getBridge().getWebView() : null;
        if (wv == null) return;
        String url = SETUP_PAGE + (query == null || query.isEmpty() ? "" : "?" + query);
        wv.post(() -> wv.loadUrl(url));
    }

    /** The setup page's "server unreachable" picker reads the install list while
     *  the page is still parsing, before onPageLoaded lets the setup gate pass:
     *  render it again now (setup.html's renderUnreachablePicker is idempotent). */
    private void renderSetupPicker(WebView view) {
        String cur = currentLoadedUrl;
        if (cur != null && cur.startsWith(SETUP_PAGE) && cur.contains("error=unreachable")) {
            view.evaluateJavascript(
                    "window.renderUnreachablePicker && window.renderUnreachablePicker()", null);
        }
    }

    /** The fail-closed notice: the dashboard channel cannot be scoped here. */
    private void showBridgeBlockedDialog(String message) {
        try {
            AlertDialog.Builder b = new AlertDialog.Builder(this)
                    .setTitle("OtoDock")
                    .setMessage(message)
                    .setCancelable(false);
            if (WEBVIEW_TOO_OLD.equals(message)) {
                b.setPositiveButton("Open Play Store", (d, w) -> openWebViewStorePage())
                        .setNegativeButton("Close", (d, w) -> finishAndRemoveTask());
            } else {
                b.setPositiveButton("OK", null);
            }
            b.show();
        } catch (Exception ignored) {}
    }

    private void openWebViewStorePage() {
        String pkg = "com.google.android.webview";
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse("market://details?id=" + pkg)));
        } catch (Exception e) {
            try {
                startActivity(new Intent(Intent.ACTION_VIEW,
                        Uri.parse("https://play.google.com/store/apps/details?id=" + pkg)));
            } catch (Exception ignored) {}
        }
    }

    /** Post a validation result back to the setup page's JS callback. */
    private void postValidationCallback(String type, String message) {
        String escaped = message.replace("\\", "\\\\").replace("'", "\\'");
        runOnUiThread(() -> {
            WebView wv = getBridge().getWebView();
            if (wv != null) {
                wv.evaluateJavascript(
                    "window._onValidationResult && window._onValidationResult('" +
                    type + "', '" + escaped + "')", null);
            }
        });
    }

    private void acquireWakeLock() {
        try {
            if (wakeLock != null && wakeLock.isHeld()) return;
            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            if (pm != null) {
                wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "OtoDock::Streaming");
                wakeLock.acquire(10 * 60 * 1000L); // 10 min safety timeout
            }
        } catch (Exception ignored) {}
    }

    private void releaseWakeLock() {
        try {
            if (wakeLock != null && wakeLock.isHeld()) {
                wakeLock.release();
            }
            wakeLock = null;
        } catch (Exception ignored) {}
    }

    private void resumeWebView() {
        runOnUiThread(() -> {
            try {
                WebView wv = getBridge().getWebView();
                if (wv != null) wv.onResume();
            } catch (Exception ignored) {}
        });
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (foldDiag != null) foldDiag.onWindowFocusChanged(hasFocus);
        if (!hasFocus) {
            // Off the UI thread: flush() blocks until the cookie store is on
            // disk, and the window loses focus in the middle of a display
            // switch. A direct execute, not a channel call: no dispatch guard.
            final long queued = SystemClock.uptimeMillis();
            NATIVE_CALLS.execute(() -> {
                long started = SystemClock.uptimeMillis();
                CookieManager.getInstance().flush();
                FoldDiagnostics.flushed(queued, started, SystemClock.uptimeMillis());
            });
        }
    }

    @Override
    public void onTopResumedActivityChanged(boolean isTopResumedActivity) {
        super.onTopResumedActivityChanged(isTopResumedActivity);
        if (foldDiag != null) foldDiag.onTopResumedActivityChanged(isTopResumedActivity);
    }

    @Override
    public void onTrimMemory(int level) {
        super.onTrimMemory(level);
        if (foldDiag != null) foldDiag.onTrimMemory(level);
    }

    @Override
    public void onDestroy() {
        if (foldDiag != null) {
            foldDiag.detach(isChangingConfigurations(), getChangingConfigurations(), sSwitchInProgress);
            foldDiag = null;
        }
        super.onDestroy();
    }

    /** The activity's own state, on every fold line. */
    private String foldFlags() {
        return "paused=" + isPaused + " streaming=" + isStreaming + " videoFullscreen=" + videoFullscreen;
    }

    /**
     * Fold/unfold (and any other in-process config change — the manifest's
     * configChanges swallows recreation, so this is the only hook that runs).
     * Two belt-and-braces recoveries for the foldable stuck-in-landscape
     * family, both no-ops on a healthy pass:
     * 1. Clear a stale video-fullscreen orientation lock — SENSOR_LANDSCAPE
     *    is the only orientation this app ever requests, and outside video
     *    fullscreen nothing should hold it.
     * 2. Re-drive the WebView's layout: fold display switches can leave the
     *    WebView surface sized for the previous display (known
     *    WebView-on-foldable stuck-surface family); requestLayout now and
     *    once more after the transition settles.
     * The fold log records the change first, then the frames that follow.
     */
    @Override
    public void onConfigurationChanged(Configuration newConfig) {
        if (foldDiag != null) foldDiag.onConfigurationChanged(newConfig);
        super.onConfigurationChanged(newConfig);
        // Capacitor's plugins re-applied their own bar styles just now.
        reapplyBars();
        try {
            if (!videoFullscreen
                    && getRequestedOrientation() == ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE) {
                setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED);
            }
        } catch (Exception ignored) {}
        try {
            final WebView wv = (getBridge() != null) ? getBridge().getWebView() : null;
            if (wv != null) {
                wv.post(() -> {
                    wv.requestLayout();
                    wv.invalidate();
                });
                wv.postDelayed(() -> {
                    wv.requestLayout();
                    wv.invalidate();
                }, 300);
            }
        } catch (Exception ignored) {}
    }

    /**
     * Shared back-navigation logic invoked by the OnBackPressedCallback
     * registered in onCreate. Asks the SPA where it is and either minimizes
     * the app (chat/setup) or lets React Router pop its history (settings /
     * agents / admin pages).
     */
    private void handleBackAction() {
        WebView webView = getBridge().getWebView();
        if (webView == null) {
            moveTaskToBack(true);
            return;
        }
        webView.evaluateJavascript(
            "(function() {" +
            "  var url = window.location.href;" +
            "  if (url.indexOf('setup.html') !== -1) {" +
            "    return url.indexOf('mode=add') !== -1 ? 'cancel-add' : 'minimize';" +
            "  }" +
            // An open dialog (a confirm, a form) is the topmost layer: Back
            // closes it as Escape does, so what is open inside it goes first
            // (a menu, the image viewer, a fullscreen video), else the dialog.
            // lib/ui/modalLayers.ts sets these while a dialog is open.
            "  if (window.__otodockDialogActive === true) {" +
            "    if (typeof window.__otodockDialogClose === 'function') {" +
            "      window.__otodockDialogClose();" +
            "    }" +
            "    return 'handled';" +
            "  }" +
            // Video fullscreen outside a dialog — Back exits fullscreen (which
            // restores padding/orientation via exitVideoFullscreen) instead of
            // minimizing. VideoPlayer.tsx wires these globals on FS enter.
            "  if (window.__otodockVideoFullscreenActive === true) {" +
            "    if (typeof window.__otodockVideoFullscreenClose === 'function') {" +
            "      window.__otodockVideoFullscreenClose();" +
            "    }" +
            "    return 'handled';" +
            "  }" +
            // Image lightbox (chat / task-run views) — when open it sets
            // these globals so the back gesture closes the overlay instead
            // of minimizing the app on chat pages. Same pattern as the
            // workspace-selection check below; ImageLightbox.tsx wires
            // `__otodockLightboxClose` to its onClose handler.
            "  if (window.__otodockLightboxActive === true) {" +
            "    if (typeof window.__otodockLightboxClose === 'function') {" +
            "      window.__otodockLightboxClose();" +
            "    }" +
            "    return 'handled';" +
            "  }" +
            // Workspace selection mode takes priority over every other back
            // behaviour. WorkspaceOverlay sets `window.__otodockWorkspaceSelectionActive`
            // and exposes an exit callback while the user is multi-selecting
            // on mobile; back/swipe should drop selection mode first instead
            // of closing the overlay.
            "  if (window.__otodockWorkspaceSelectionActive === true) {" +
            "    if (typeof window.__otodockWorkspaceExitSelection === 'function') {" +
            "      window.__otodockWorkspaceExitSelection();" +
            "    }" +
            "    return 'handled';" +
            "  }" +
            "  var p = window.location.pathname;" +
            "  var s = window.location.search || '';" +
            // Workspace overlay layers (chat AND task-run views) push search
            // params via React Router. Let history.back() pop them before
            // falling through to minimize, otherwise the workspace and its
            // file-preview portal become inaccessible to the system back
            // gesture on chat pages.
            "  if (s.indexOf('ws_preview=') !== -1 || s.indexOf('ws=1') !== -1) {" +
            "    window.history.back();" +
            "    return 'back';" +
            "  }" +
            // Chat is the app's home surface, so Back normally minimizes. But a
            // chat reached by navigating INTO it (e.g. opening a conversation
            // from the Conversations tab, which redirects to /chat/:agent/:id)
            // has in-app history to pop — React Router tracks depth in
            // history.state.idx (0 = cold-start home). Pop it instead of exiting
            // so Back returns to where the user came from.
            "  if (p === '/' || p.startsWith('/chat/')) {" +
            "    var idx = (window.history.state && window.history.state.idx) || 0;" +
            "    if (idx > 0) { window.history.back(); return 'back'; }" +
            "    return 'minimize';" +
            "  }" +
            "  window.history.back();" +
            "  return 'back';" +
            "})()",
            value -> {
                if ("\"minimize\"".equals(value)) {
                    runOnUiThread(() -> moveTaskToBack(true));
                } else if ("\"cancel-add\"".equals(value)) {
                    runOnUiThread(() -> {
                        if (installStore.active() != null) switchRecreate();
                        else moveTaskToBack(true);
                    });
                }
            }
        );
    }
}
