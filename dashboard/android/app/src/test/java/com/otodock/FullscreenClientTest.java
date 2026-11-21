package com.otodock;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import android.view.View;
import android.webkit.WebChromeClient;
import com.getcapacitor.BridgeWebChromeClient;
import org.junit.Test;

/**
 * Full screen in the app. Android's WebView offers it to a page only when
 * the WebChromeClient's class declares onShowCustomView and onHideCustomView,
 * which it looks for by name; without both, no video goes full screen, the
 * dashboard's player or a frame's own. The rule is run here on the class the
 * app installs; the shrunk release build is held to it by the build's
 * checkReleaseFullscreen, which reads the dex R8 wrote.
 */
public class FullscreenClientTest {

    /** The WebView's own rule (its doesSupportFullscreen): walk the client's
     *  classes up to WebChromeClient, both methods declared on the way. */
    private static boolean webViewOffersFullscreen(Class<?> client) {
        boolean show = false;
        boolean hide = false;
        for (Class<?> c = client; c != null && c != WebChromeClient.class && !(show && hide);
                c = c.getSuperclass()) {
            if (!show) {
                try {
                    c.getDeclaredMethod("onShowCustomView", View.class, WebChromeClient.CustomViewCallback.class);
                    show = true;
                } catch (NoSuchMethodException e) {
                    // not on this class
                }
            }
            if (!hide) {
                try {
                    c.getDeclaredMethod("onHideCustomView");
                    hide = true;
                } catch (NoSuchMethodException e) {
                    // not on this class
                }
            }
        }
        return show && hide;
    }

    private static class OnlyShows extends WebChromeClient {
        @Override
        public void onShowCustomView(View view, CustomViewCallback callback) {}
    }

    @Test
    public void theAppsClientDeclaresBothMethodsTheWebViewLooksFor() throws Exception {
        assertTrue(webViewOffersFullscreen(FullscreenWebChromeClient.class));
        // Itself, not through Capacitor's: the app's must never call super.
        FullscreenWebChromeClient.class.getDeclaredMethod(
                "onShowCustomView", View.class, WebChromeClient.CustomViewCallback.class);
        FullscreenWebChromeClient.class.getDeclaredMethod("onHideCustomView");
        assertEquals(BridgeWebChromeClient.class, FullscreenWebChromeClient.class.getSuperclass());
        assertFalse("one of the two is not enough", webViewOffersFullscreen(OnlyShows.class));
        assertFalse(webViewOffersFullscreen(WebChromeClient.class));
    }

    /** Capacitor's onShowCustomView answers the callback at once, which ends
     *  the full screen it was asked for: the app's two never call it. */
    @Test
    public void theAppsClientHandsTheViewToTheActivityAndNeverCallsCapacitors() throws Exception {
        String client = SourceText.javaSource("FullscreenWebChromeClient");
        assertEquals("host.showCustomView(view, callback);",
                SourceText.body(client, "public void onShowCustomView(").trim());
        assertEquals("host.hideCustomView();", SourceText.body(client, "public void onHideCustomView(").trim());
        assertFalse(client.contains("super.on"));

        String capacitors = SourceText.withoutComments(SourceText.read(
                "../../node_modules/@capacitor/android/capacitor/src/main/java/com/getcapacitor/BridgeWebChromeClient.java"));
        assertTrue("Capacitor's still ends the full screen at once: if this fails, read its new code "
                        + "before the app's client relies on anything of it",
                SourceText.body(capacitors, "public void onShowCustomView(").contains("callback.onCustomViewHidden();"));
    }

    /** Capacitor sets its own client while it builds the bridge, after the
     *  plugins load: one set from a plugin's load is overwritten, and one set
     *  after the activity started cannot register its launchers. */
    @Test
    public void theClientIsSetInOnCreateAfterCapacitorSetItsOwn() throws Exception {
        String main = SourceText.javaSource("MainActivity");
        String set = "wv.setWebChromeClient(new FullscreenWebChromeClient(getBridge(),";
        assertEquals(1, SourceText.count(main, "setWebChromeClient("));
        String onCreate = SourceText.body(main, "protected void onCreate(Bundle savedInstanceState)");
        assertTrue(onCreate.indexOf("super.onCreate(savedInstanceState);") >= 0);
        assertTrue(onCreate.indexOf(set) > onCreate.indexOf("super.onCreate(savedInstanceState);"));
        assertFalse(SourceText.body(main, "void onBridgeWebViewReady(").contains("WebChromeClient"));

        String bridge = SourceText.withoutComments(SourceText.read(
                "../../node_modules/@capacitor/android/capacitor/src/main/java/com/getcapacitor/Bridge.java"));
        assertEquals("a second set in Capacitor's bridge would replace the app's client",
                1, SourceText.count(bridge, "setWebChromeClient("));
    }

    /** One place hides and shows the bars, for the page's calls and for the
     *  WebView's view, and the state is touched on the UI thread only. */
    @Test
    public void theBarsHaveOneOwner() throws Exception {
        String main = SourceText.javaSource("MainActivity");
        assertEquals("runOnUiThread(() -> applyVideoFullscreen(videoFs.pageEntered()));",
                SourceText.body(main, "private void enterVideoFullscreen(boolean landscape)").trim());
        assertEquals("runOnUiThread(() -> applyVideoFullscreen(videoFs.pageLeft()));",
                SourceText.body(main, "private void exitVideoFullscreen()").trim());
        assertEquals(1, SourceText.count(main, "ctl.hide(WindowInsetsCompat.Type.systemBars())"));
        assertEquals(1, SourceText.count(main, "ctl.show(WindowInsetsCompat.Type.systemBars())"));
        String apply = SourceText.body(main, "private void applyVideoFullscreen(boolean changed)");
        assertTrue(apply.contains("ctl.hide(WindowInsetsCompat.Type.systemBars())"));
        assertTrue(apply.contains("ctl.show(WindowInsetsCompat.Type.systemBars())"));
        assertEquals("the flag is written in one place beside its declaration", 2, SourceText.count(main, "videoFullscreen = "));
        assertTrue(apply.contains("videoFullscreen = on;"));
        assertTrue(SourceText.body(main, "private void showCustomView(").contains("applyVideoFullscreen(videoFs.viewShown());"));
        assertTrue(SourceText.body(main, "private void hideCustomView(").contains("applyVideoFullscreen(videoFs.viewHidden());"));
        assertTrue(SourceText.body(main, "public void onPageCommitVisible(").contains("applyVideoFullscreen(videoFs.newDocument());"));
    }

    /** A view that cannot be shown answers the callback at once, as
     *  Capacitor's client does, and nothing is thrown at the WebView. */
    @Test
    public void aViewThatCannotBeShownTellsTheWebView() throws Exception {
        String show = SourceText.body(SourceText.javaSource("MainActivity"), "private void showCustomView(");
        int refusal = show.indexOf("if (customView != null || isFinishing() || isDestroyed())");
        int add = show.indexOf("container.addView(view,");
        int caught = show.indexOf("catch (RuntimeException e)");
        int kept = show.indexOf("customView = view;");
        assertTrue(refusal >= 0 && add > refusal && caught > add && kept > caught);
        assertEquals(2, SourceText.count(show, "callback.onCustomViewHidden();"));
        assertFalse("the WebView under the view stays visible", show.contains("setVisibility"));
    }

    /** What is full screen is what the person sees: Back leaves it before
     *  the page is asked anything, a frame's own player included. */
    @Test
    public void backLeavesTheFullScreenViewFirst() throws Exception {
        String back = SourceText.body(SourceText.javaSource("MainActivity"), "private void handleBackAction()");
        int view = back.indexOf("leave.onCustomViewHidden();");
        assertTrue(view >= 0);
        assertTrue(view < back.indexOf("evaluateJavascript("));
        assertTrue(back.indexOf("WebChromeClient.CustomViewCallback leave = customViewCallback;") < view);
    }

    /** R8 removes a method that only calls its parent, which is Capacitor's
     *  onHideCustomView: the rule keeps both, and every R8 run is checked on
     *  the dex it wrote before anything is packaged from it. */
    @Test
    public void theShrinkerKeepsBothMethodsAndEveryR8RunIsChecked() throws Exception {
        String rules = SourceText.read("proguard-rules.pro");
        assertTrue(rules.contains("-keepclassmembers class * extends android.webkit.WebChromeClient {\n"
                + "    public void onShowCustomView(android.view.View, android.webkit.WebChromeClient$CustomViewCallback);\n"
                + "    public void onHideCustomView();\n}"));
        assertTrue(SourceText.withoutComments(SourceText.read("build.gradle"))
                .contains("apply from: 'release-checks.gradle'"));
        String checks = SourceText.withoutComments(SourceText.read("release-checks.gradle"));
        assertTrue(checks.contains("tasks.register('checkReleaseFullscreen')"));
        assertTrue(checks.contains("'com.otodock.FullscreenWebChromeClient'"));
        assertTrue(checks.contains("'com.getcapacitor.BridgeWebChromeClient'"));
        assertTrue(checks.contains("tasks.matching { it.name == 'minifyReleaseWithR8' }.configureEach {\n"
                + "    finalizedBy 'checkReleaseFullscreen'\n}"));
        assertTrue(checks.contains("tasks.matching { it.name in ['packageRelease', 'packageReleaseBundle'] }.configureEach {\n"
                + "    dependsOn 'checkReleaseFullscreen'\n}"));
    }
}
