package com.otodock;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import android.content.Context;
import android.content.SharedPreferences;
import android.database.Cursor;
import android.os.Build;
import android.os.SystemClock;
import android.provider.MediaStore;
import android.webkit.CookieManager;
import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

/**
 * On a device: a sandboxed frame inside the dashboard page (how display_ui
 * artifacts and app frames render) reaches no native surface, while the
 * top-level document of the active install still reaches the channel. The
 * boundary is same-origin script: a same-origin child frame (unsandboxed, or
 * sandboxed with allow-same-origin, the editor frame's shape) is refused on
 * its own object but reaches the channel through the top document's.
 *
 * Runs only with a device or emulator attached (connectedAndroidTest); it is
 * not part of the build gate. It snapshots the app's saved installs and
 * restores them afterwards. The app asks once for the battery-optimization
 * exemption and the full-screen-intent permission on a cold start: grant both
 * on the device first, or the launch waits behind those screens.
 */
@RunWith(AndroidJUnit4.class)
public class NativeBridgeFrameScopeTest {

    private static final String PREFS = "otodock_prefs";
    private static final String PROBE_LABEL = "Frame scope probe";

    private Context ctx;
    private Map<String, ?> savedPrefs;
    private ServerSocket server;
    private String origin;
    private volatile String page = "";
    // Served for FRAME_PATH, so a frame can load a real same-origin document.
    private volatile String framePage = "";
    private static final String FRAME_PATH = "/frame";
    private volatile int served = 0;

    @Before
    public void setUp() throws Exception {
        ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        SharedPreferences prefs = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        savedPrefs = new HashMap<>(prefs.getAll());
        prefs.edit().clear().commit();
        server = new ServerSocket(0, 10, InetAddress.getByName("127.0.0.1"));
        origin = "http://127.0.0.1:" + server.getLocalPort();
        Thread responder = new Thread(this::serve, "frame-scope-responder");
        responder.setDaemon(true);
        responder.start();
    }

    @After
    public void tearDown() throws Exception {
        server.close();
        CookieManager.getInstance().setCookie(origin, "zz_frame_probe=; Max-Age=0; Path=/");
        CookieManager.getInstance().flush();
        SharedPreferences.Editor e = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().clear();
        for (Map.Entry<String, ?> entry : savedPrefs.entrySet()) {
            Object v = entry.getValue();
            if (v instanceof String) e.putString(entry.getKey(), (String) v);
            else if (v instanceof Boolean) e.putBoolean(entry.getKey(), (Boolean) v);
            else if (v instanceof Long) e.putLong(entry.getKey(), (Long) v);
            else if (v instanceof Integer) e.putInt(entry.getKey(), (Integer) v);
            else if (v instanceof Float) e.putFloat(entry.getKey(), (Float) v);
            else if (v instanceof Set) {
                @SuppressWarnings("unchecked") Set<String> set = (Set<String>) v;
                e.putStringSet(entry.getKey(), set);
            }
        }
        e.commit();
    }

    /** Answers FRAME_PATH with the frame page and every other request with the
     *  current page (one HTTP/1.1 response per connection). */
    private void serve() {
        while (!server.isClosed()) {
            try (Socket s = server.accept()) {
                BufferedReader in = new BufferedReader(new InputStreamReader(s.getInputStream(), StandardCharsets.UTF_8));
                String requestLine = in.readLine();
                String line;
                while ((line = in.readLine()) != null && !line.isEmpty()) { /* request headers */ }
                boolean frame = requestLine != null && requestLine.startsWith("GET " + FRAME_PATH + " ");
                byte[] body = (frame ? framePage : page).getBytes(StandardCharsets.UTF_8);
                OutputStream out = s.getOutputStream();
                out.write(("HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                        + "Content-Length: " + body.length + "\r\nConnection: close\r\n\r\n")
                        .getBytes(StandardCharsets.UTF_8));
                out.write(body);
                out.flush();
                served++;
            } catch (Exception ignored) {
                // closed in tearDown, or a client that went away
            }
        }
    }

    private InstallStore.Install[] seedInstalls() {
        InstallStore store = new InstallStore(ctx);
        InstallStore.Install probe = store.add(origin, PROBE_LABEL);
        InstallStore.Install other = store.add("https://second.example.invalid", "Second");
        store.setFavorite(probe.id);
        store.setActive(probe.id);
        return new InstallStore.Install[]{probe, other};
    }

    private void loadPage(String html) throws Exception {
        page = html;
        try (ActivityScenario<MainActivity> ignored = ActivityScenario.launch(MainActivity.class)) {
            long deadline = SystemClock.elapsedRealtime() + 20_000;
            while (served == 0 && SystemClock.elapsedRealtime() < deadline) Thread.sleep(100);
            assertTrue("the probe page was served", served > 0);
            Thread.sleep(4_000);
        }
    }

    private static String post(String method, String args) {
        return "OtoDockNative.postMessage(JSON.stringify({m:'" + method + "',a:" + args + "}))";
    }

    @Test
    public void aSandboxedFrameReachesNoNativeSurface() throws Exception {
        InstallStore.Install[] installs = seedInstalls();
        String frameScript = "<script>"
                + "try{" + post("renameInstallation", "['" + installs[0].id + "','Renamed by a frame']") + "}catch(e){}"
                + "try{OtoDockNative.postMessage(JSON.stringify({m:'saveImageFromBase64',a:['zz-frame-probe.png','image/png']})+String.fromCharCode(10)+'QUJD')}catch(e){}"
                + "try{Android.setFavorite('" + installs[1].id + "')}catch(e){}"
                + "try{CapacitorCookiesAndroidInterface.setCookie('','zz_frame_probe=1')}catch(e){}"
                + "</script>";
        loadPage("<!doctype html><html><body><p>top document</p>"
                + "<iframe sandbox=\"allow-scripts\" srcdoc=\"" + frameScript.replace("\"", "&quot;") + "\"></iframe>"
                + "</body></html>");

        InstallStore after = new InstallStore(ctx);
        assertEquals(PROBE_LABEL, after.byId(installs[0].id).label);
        assertEquals(installs[0].id, after.favorite().id);
        String cookies = CookieManager.getInstance().getCookie(origin);
        assertFalse(cookies != null && cookies.contains("zz_frame_probe"));
        assertFalse(downloadExists("zz-frame-probe"));
    }

    @Test
    public void theTopDocumentReachesTheChannel() throws Exception {
        InstallStore.Install[] installs = seedInstalls();
        loadPage("<!doctype html><html><body><script>"
                + post("renameInstallation", "['" + installs[0].id + "','Renamed by the top document']")
                + "</script></body></html>");
        assertEquals("Renamed by the top document", new InstallStore(ctx).byId(installs[0].id).label);
    }

    /** The frame script: its own object first (refused, the frame is not the
     *  main frame), then the top document's (reported as the main frame of the
     *  active origin, so honoured). */
    private static String sameOriginFrameScript(InstallStore.Install[] installs, String label) {
        return "<!doctype html><html><body><script>"
                + "try{" + post("setFavorite", "['" + installs[1].id + "']") + "}catch(e){}"
                + "try{top." + post("renameInstallation", "['" + installs[0].id + "','" + label + "']") + "}catch(e){}"
                + "</script></body></html>";
    }

    @Test
    public void aSameOriginFrameReachesTheChannelOnlyThroughTheTopDocument() throws Exception {
        InstallStore.Install[] installs = seedInstalls();
        framePage = sameOriginFrameScript(installs, "Renamed through the top document");
        loadPage("<!doctype html><html><body><p>top document</p>"
                + "<iframe src=\"" + FRAME_PATH + "\"></iframe></body></html>");

        InstallStore after = new InstallStore(ctx);
        assertEquals("the frame's own object is refused", installs[0].id, after.favorite().id);
        assertEquals("the top document's object answers same-origin script",
                "Renamed through the top document", after.byId(installs[0].id).label);
    }

    @Test
    public void aSameOriginSandboxedFrameReachesTheChannelOnlyThroughTheTopDocument() throws Exception {
        InstallStore.Install[] installs = seedInstalls();
        framePage = sameOriginFrameScript(installs, "Renamed by the editor-shaped frame");
        loadPage("<!doctype html><html><body><p>top document</p>"
                + "<iframe sandbox=\"allow-scripts allow-same-origin\" src=\"" + FRAME_PATH + "\"></iframe>"
                + "</body></html>");

        InstallStore after = new InstallStore(ctx);
        assertEquals("the frame's own object is refused", installs[0].id, after.favorite().id);
        assertEquals("the top document's object answers same-origin script",
                "Renamed by the editor-shaped frame", after.byId(installs[0].id).label);
    }

    private boolean downloadExists(String prefix) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return false;
        try (Cursor c = ctx.getContentResolver().query(MediaStore.Downloads.EXTERNAL_CONTENT_URI,
                new String[]{MediaStore.Downloads.DISPLAY_NAME},
                MediaStore.Downloads.DISPLAY_NAME + " LIKE ?", new String[]{prefix + "%"}, null)) {
            return c != null && c.getCount() > 0;
        }
    }
}
