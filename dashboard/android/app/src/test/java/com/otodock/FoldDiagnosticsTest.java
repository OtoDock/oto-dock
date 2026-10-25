package com.otodock;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.junit.Test;

/** The fold log's wiring, read from the source: attached and detached with
 *  the activity, each thing registered removed, the cookie flush off the UI
 *  thread, the watchdog bound to a visible window, every line at a level the
 *  phone shows. */
public class FoldDiagnosticsTest {

    @Test
    public void theLogIsAttachedWithTheActivityAndDetachedWithItsReason() throws Exception {
        String main = SourceText.javaSource("MainActivity");
        assertTrue(SourceText.body(main, "protected void onCreate(").contains("FoldDiagnostics.attach("));
        String destroy = SourceText.body(main, "public void onDestroy(");
        assertTrue(destroy.contains(".detach("));
        assertTrue("the reason and the switch flag", destroy.contains("isChangingConfigurations()")
                && destroy.contains("getChangingConfigurations()") && destroy.contains("sSwitchInProgress"));
        String config = SourceText.body(main, "public void onConfigurationChanged(");
        assertTrue("the log sees the change before Capacitor's plugins do",
                config.indexOf(".onConfigurationChanged(newConfig)") < config.indexOf("super.onConfigurationChanged("));
        for (String hook : new String[]{"public void onTopResumedActivityChanged(", "public void onTrimMemory("}) {
            assertTrue(hook, SourceText.body(main, hook).contains("foldDiag."));
        }
        String dispatch = SourceText.body(main, "void dispatchNative(");
        assertTrue(dispatch.contains("case \"foldNote\":"));
        assertTrue("read once off the UI thread", dispatch.contains("FoldDiagnostics diag = foldDiag;"));
        assertTrue(main.contains("private volatile FoldDiagnostics foldDiag;"));
    }

    /** Every register or add in attach has its remove in detach, and the
     *  display listener is registered only where a display change can
     *  matter, and removed only when it was registered. */
    @Test
    public void everythingAttachRegistersDetachRemoves() throws Exception {
        String diag = SourceText.javaSource("FoldDiagnostics");
        String attach = SourceText.body(diag, "static FoldDiagnostics attach(");
        String detach = SourceText.body(diag, "void detach(");
        String[][] pairs = {
                {"registerDisplayListener(d.displayListener", "unregisterDisplayListener(displayListener)"},
                {"addObserver(d.lifecycle)", "removeObserver(lifecycle)"},
                {"addOnAttachStateChangeListener(d.attachListener)", "removeOnAttachStateChangeListener(attachListener)"},
        };
        for (String[] pair : pairs) {
            assertTrue(pair[0], attach.contains(pair[0]));
            assertTrue(pair[1], detach.contains(pair[1]));
        }
        assertTrue("the watchdog goes with the instance", detach.contains("handler.removeCallbacksAndMessages(null)"));
        assertTrue(attach.contains("if (d.displaysWatched) {"));
        assertTrue(detach.contains("if (displaysWatched) displayManager.unregisterDisplayListener("));
        String watches = SourceText.body(diag, "private static boolean watchesDisplays(");
        assertTrue(watches.contains("ApplicationInfo.FLAG_DEBUGGABLE"));
        assertTrue(watches.contains("PackageManager.FEATURE_SENSOR_HINGE_ANGLE"));
    }

    /** A pending visual-state callback of an earlier change logs nothing; a
     *  frame of an earlier change does not quiet the watchdog. */
    @Test
    public void onlyTheLatestChangesFrameAndVisualStateCount() throws Exception {
        String diag = SourceText.javaSource("FoldDiagnostics");
        assertTrue(SourceText.body(diag, "private void armFrameWatch(").contains("if (id == visualRequest)"));
        assertTrue(SourceText.body(diag, "private void frameLine(").contains("if (since == configChangedAt) frameSeen = true;"));
    }

    @Test
    public void theCookieFlushOnFocusLossLeavesTheUiThread() throws Exception {
        String focus = SourceText.body(SourceText.javaSource("MainActivity"), "public void onWindowFocusChanged(");
        int execute = focus.indexOf("NATIVE_CALLS.execute(");
        int flush = focus.indexOf("CookieManager.getInstance().flush()");
        assertTrue("queued on the executor", execute >= 0);
        assertTrue("the flush is inside the queued task", flush > execute);
        assertFalse("not a channel call", focus.contains("dispatchNative"));
        assertTrue("timed", focus.contains("FoldDiagnostics.flushed("));
    }

    @Test
    public void everyLineIsAtALevelThePhoneShows() throws Exception {
        String diag = SourceText.javaSource("FoldDiagnostics");
        Matcher m = Pattern.compile("Log\\.(\\w)\\(").matcher(diag);
        int lines = 0;
        while (m.find()) {
            lines++;
            String level = m.group(1);
            assertTrue("Log." + level, level.equals("i") || level.equals("w"));
        }
        assertTrue(lines > 10);
    }

    @Test
    public void aDisplayLineComesOnlyWhenACompareFieldChanged() throws Exception {
        String diag = SourceText.javaSource("FoldDiagnostics");
        String log = SourceText.body(diag, "private void logDisplay(");
        assertTrue(log.contains("if (now.sameAs(last)) return;"));
        assertTrue(log.contains("lastByDisplay.put(displayId, now)"));
    }

    @Test
    public void theFrameWatchdogSpeaksOnlyForAVisibleWindow() throws Exception {
        String diag = SourceText.javaSource("FoldDiagnostics");
        String arm = SourceText.body(diag, "private void armFrameWatch(");
        assertTrue(arm.contains("registerFrameCommitCallback("));
        assertTrue(arm.contains("postVisualStateCallback("));
        assertTrue(arm.contains("webView.getWindowVisibility() == View.VISIBLE"));
    }

    @Test
    public void thePagesNoteIsCappedAndTheRendererClientIsFeatureChecked() throws Exception {
        String diag = SourceText.javaSource("FoldDiagnostics");
        String page = SourceText.body(diag, "void page(");
        assertTrue(page.contains("offsetByCodePoints(0, MAX_PAGE_CHARS)"));
        assertTrue("every control character becomes a space", page.contains("Character.isISOControl(c) ? ' ' : c"));
        assertEquals(200, FoldDiagnostics.MAX_PAGE_CHARS);
        String attach = SourceText.body(diag, "static FoldDiagnostics attach(");
        int check = attach.indexOf("WebViewFeature.isFeatureSupported(WebViewFeature.WEB_VIEW_RENDERER_CLIENT_BASIC_USAGE)");
        int client = attach.indexOf("setWebViewRenderProcessClient(");
        assertTrue(check >= 0 && client > check);
    }
}
