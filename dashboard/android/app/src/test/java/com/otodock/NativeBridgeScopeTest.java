package com.otodock;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.junit.Before;
import org.junit.Test;

/**
 * The dashboard's native calls reach the app only through the
 * origin-scoped message channel, and the one raw interface left serves the
 * bundled setup page alone. Source-level, so it runs without a device.
 */
public class NativeBridgeScopeTest {

    /** Every method the dashboard may post. */
    private static final Set<String> CHANNEL_METHODS = new HashSet<>(Arrays.asList(
            "setStreaming", "setContainerColor", "enterVideoFullscreen", "exitVideoFullscreen",
            "flushCookies", "dashboardReady", "firstScreen", "setAuthInProgress", "setSwitchBusy",
            "openAuthBrowser", "saveImageFromBase64", "getInstallations", "setFavorite",
            "renameInstallation", "switchInstallation", "switchToInstall", "openAddInstallation",
            "removeInstallation", "disconnectServer", "openPrivacyPolicy", "foldNote"));

    private static final Set<String> SETUP_METHODS = new HashSet<>(Arrays.asList(
            "addInstallation", "switchFromSetup", "cancelAddInstallation",
            "getInstallations", "setFavorite", "openPrivacyPolicy"));

    private String main;

    @Before
    public void readSource() throws Exception {
        main = SourceText.javaSource("MainActivity");
    }

    @Test
    public void theOnlyRawInterfaceIsTheSetupObject() {
        assertEquals("exactly one raw interface", 1, SourceText.count(main, "addJavascriptInterface("));
        assertTrue(main.contains("addJavascriptInterface(new SetupBridge(), \"Android\")"));
    }

    @Test
    public void theSetupObjectCarriesOnlyTheSetupMethodsEachGated() {
        String setup = SourceText.body(main, "class SetupBridge");
        Matcher m = Pattern.compile("@JavascriptInterface\\s+public\\s+[\\w<>]+\\s+(\\w+)\\s*\\(").matcher(setup);
        Set<String> found = new TreeSet<>();
        while (m.find()) {
            String name = m.group(1);
            found.add(name);
            String methodBody = SourceText.blockAfter(setup, m.end()).trim();
            assertTrue(name + " must open with the setup gate",
                    methodBody.startsWith("if (!callerIsSetup()) return"));
        }
        assertEquals(new TreeSet<>(SETUP_METHODS), found);
        assertEquals("no bridge method outside the setup object",
                SETUP_METHODS.size(), SourceText.count(main, "@JavascriptInterface"));
    }

    @Test
    public void capacitorsCookieInterfaceIsRemovedBeforeTheChannelIsAdded() {
        String ready = SourceText.body(main, "void onBridgeWebViewReady(");
        int removal = ready.indexOf("removeJavascriptInterface(\"CapacitorCookiesAndroidInterface\")");
        int channel = ready.indexOf("addWebMessageListener(");
        assertTrue("the cookie interface is removed", removal >= 0);
        assertTrue("the channel is registered", channel >= 0);
        assertTrue("removal first", removal < channel);
        assertTrue("the setup object is added there too", ready.contains("addJavascriptInterface("));
    }

    @Test
    public void theChannelIsScopedToTheActiveOriginAndTheMainFrame() {
        String ready = SourceText.body(main, "void onBridgeWebViewReady(");
        assertTrue(ready.contains("Collections.singleton(activeOrigin)"));
        String listener = SourceText.body(main, "void onNativeMessage(");
        assertTrue(listener.contains("if (!isMainFrame)"));
        assertTrue(listener.contains("NativeBridgePolicy.sameOrigin(activeOrigin"));
        assertTrue(listener.contains("WebMessageCompat.TYPE_STRING"));
        assertTrue(listener.contains("NativeBridgePolicy.MAX_MESSAGE_CHARS"));
        assertTrue(listener.contains("NativeBridgePolicy.isDashboardPage("));
        assertTrue(main.contains("WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)"));
    }

    @Test
    public void pageAndRequestHostsAreComparedWithTheBootedOriginsHost() {
        for (String fn : new String[]{"private boolean isAuthSiblingHost(", "private void maybeBindInstallId(",
                "public void onReceivedError("}) {
            String body = SourceText.body(main, fn);
            assertTrue(fn + " uses the booted origin's host", body.contains("activeHost()"));
            assertFalse(fn + " never the saved URL's host as typed", body.contains("extractHost(active.url)"));
        }
        String bind = SourceText.body(main, "private void maybeBindInstallId(");
        assertTrue(bind.contains("origin + \"/v1/app/identity\""));
        assertTrue(bind.contains("getCookie(origin)"));
        assertTrue(SourceText.body(main, "private String activeHost(").contains("NativeBridgePolicy.host("));
    }

    @Test
    public void theDispatchTableIsExactlyTheChannelMethods() {
        String dispatch = SourceText.body(main, "void dispatchNative(");
        Matcher m = Pattern.compile("case \"(\\w+)\":").matcher(dispatch);
        Set<String> found = new TreeSet<>();
        while (m.find()) found.add(m.group(1));
        assertEquals(new TreeSet<>(CHANNEL_METHODS), found);
    }

    @Test
    public void aSaveBelowApi29WaitsForTheStoragePermission() {
        String save = SourceText.body(main, "private void saveImageFromBase64(");
        int ask = save.indexOf("if (needsStorageAsk())");
        int write = save.indexOf("new Thread(");
        assertTrue("the question comes before the write", ask >= 0 && write > ask);
        assertTrue(SourceText.body(main, "private void askForStorage(").contains("if (storageAskOpen) return;"));
        String result = SourceText.body(main, "public void onRequestPermissionsResult(");
        assertTrue(result.contains("if (grantResults.length == 0) {"));
        assertTrue(result.contains("saveImageFromBase64(save[0], save[1], save[2])"));
        assertTrue(result.contains("startDownload(download[0], download[1], download[2], download[3])"));
    }

    @Test
    public void theCleartextRuleIsThePolicys() {
        String add = SourceText.body(SourceText.body(main, "class SetupBridge"), "public void addInstallation(");
        assertTrue(add.contains("NativeBridgePolicy.serverUrl(rawUrl)"));
        assertFalse(main.contains("isPrivateHost"));
    }

    @Test
    public void aSavedInstallBootsOnlyIfThePolicyAllowsIt() {
        String load = SourceText.body(main, "protected void load()");
        assertTrue(load.contains("!NativeBridgePolicy.bootable(origin)"));
    }

    @Test
    public void thePrivacyPolicyOpensOnlyItsFixedAddress() {
        String open = SourceText.body(main, "private void openPrivacyPolicyInBrowser(");
        assertTrue(open.contains("Uri.parse(NativeBridgePolicy.PRIVACY_POLICY_URL)"));
        assertTrue(open.contains("CATEGORY_BROWSABLE"));
        String dispatch = SourceText.body(main, "void dispatchNative(");
        assertTrue("the channel passes no address",
                dispatch.contains("case \"openPrivacyPolicy\": openPrivacyPolicyInBrowser(); break;"));
        String setup = SourceText.body(SourceText.body(main, "class SetupBridge"), "public void openPrivacyPolicy(");
        assertTrue(setup.contains("openPrivacyPolicyInBrowser();"));
    }

    @Test
    public void getServerUrlIsGone() {
        assertFalse(main.contains("getServerUrl"));
    }

    @Test
    public void theServerUrlIsTheOriginAndTheRouteRidesTheStartPath() {
        String load = SourceText.body(main, "protected void load()");
        assertTrue(load.contains(".setServerUrl(origin)"));
        assertTrue(load.contains(".setStartPath("));
        assertFalse(load.contains("active.url + active.lastRoute"));
    }

    @Test
    public void theChannelPluginIsRegisteredBeforeTheBridgeIsBuilt() throws Exception {
        String onCreate = SourceText.body(main, "protected void onCreate(");
        int reg = onCreate.indexOf("registerPlugin(NativeChannelPlugin.class)");
        int sup = onCreate.indexOf("super.onCreate(");
        assertTrue(reg >= 0 && sup > reg);
        String plugin = SourceText.javaSource("NativeChannelPlugin");
        assertTrue(plugin.contains("onBridgeWebViewReady("));
        assertFalse("nothing on the plugin is callable from a page", plugin.contains("@PluginMethod"));
    }
}
