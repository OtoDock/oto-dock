package com.otodock;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.io.File;
import java.util.Set;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import javax.xml.parsers.DocumentBuilderFactory;
import org.junit.Test;
import org.w3c.dom.Document;
import org.w3c.dom.Element;
import org.w3c.dom.Node;
import org.w3c.dom.NodeList;

/** The manifest, the provider paths, the release
 *  build type, the lock-screen versions and the Cordova access rule. */
public class AppManifestHardeningTest {

    private static final String ANDROID_NS = "http://schemas.android.com/apk/res/android";

    private static Document xml(String path) throws Exception {
        DocumentBuilderFactory f = DocumentBuilderFactory.newInstance();
        f.setNamespaceAware(true);
        return f.newDocumentBuilder().parse(new File(path));
    }

    @Test
    public void theDeepLinkFilterNamesOnlyTheAuthAndOauthHosts() throws Exception {
        Document manifest = xml("src/main/AndroidManifest.xml");
        NodeList filters = manifest.getElementsByTagName("intent-filter");
        int viewFilters = 0;
        for (int i = 0; i < filters.getLength(); i++) {
            Element filter = (Element) filters.item(i);
            if (!hasChild(filter, "action", "android.intent.action.VIEW")) continue;
            viewFilters++;
            NodeList data = filter.getElementsByTagName("data");
            Set<String> hosts = new TreeSet<>();
            for (int j = 0; j < data.getLength(); j++) {
                Element d = (Element) data.item(j);
                assertEquals("otodock", d.getAttributeNS(ANDROID_NS, "scheme"));
                String host = d.getAttributeNS(ANDROID_NS, "host");
                assertFalse("no host-less otodock data element", host.isEmpty());
                hosts.add(host);
            }
            assertEquals(new TreeSet<>(java.util.Arrays.asList("auth", "oauth")), hosts);
        }
        assertEquals(1, viewFilters);
    }

    private static boolean hasChild(Element parent, String tag, String name) {
        NodeList list = parent.getElementsByTagName(tag);
        for (int i = 0; i < list.getLength(); i++) {
            if (name.equals(((Element) list.item(i)).getAttributeNS(ANDROID_NS, "name"))) return true;
        }
        return false;
    }

    private static final String TOOLS_NS = "http://schemas.android.com/tools";

    /** The permissions the manifest declares, or (removed = true) the ones it
     *  strips from every library at the merge (tools:node="remove"). */
    private static Set<String> permissions(boolean removed) throws Exception {
        NodeList list = xml("src/main/AndroidManifest.xml").getElementsByTagName("uses-permission");
        Set<String> names = new TreeSet<>();
        for (int i = 0; i < list.getLength(); i++) {
            Element e = (Element) list.item(i);
            if ("remove".equals(e.getAttributeNS(TOOLS_NS, "node")) == removed) {
                names.add(e.getAttributeNS(ANDROID_NS, "name"));
            }
        }
        return names;
    }

    private static Set<String> permissions() throws Exception {
        return permissions(false);
    }

    /** Attaching from the library goes through the system picker, which hands
     *  back a grant for the picked file, and Save writes to Downloads: no
     *  media-read permission (Play refuses it to an app that is not a gallery),
     *  and the merge strips one a library would add. */
    @Test
    public void theAppReadsNoMediaLibrary() throws Exception {
        Set<String> names = permissions();
        Set<String> removed = permissions(true);
        assertTrue(names.contains("android.permission.CAMERA"));
        for (String media : new String[]{"READ_MEDIA_IMAGES", "READ_MEDIA_VIDEO",
                "READ_MEDIA_VISUAL_USER_SELECTED", "READ_EXTERNAL_STORAGE"}) {
            assertFalse(media, names.contains("android.permission." + media));
            assertTrue(media + " is removed at the merge", removed.contains("android.permission." + media));
        }
    }

    /** Firebase Messaging starts with no token; the push plugin's register()
     *  (called by the dashboard only after notifications are allowed) turns the
     *  automatic initialisation on. A plugin bump that stops doing so fails here. */
    @Test
    public void firebaseWaitsUntilNotificationsAreAllowed() throws Exception {
        NodeList list = xml("src/main/AndroidManifest.xml").getElementsByTagName("meta-data");
        String autoInit = null;
        for (int i = 0; i < list.getLength(); i++) {
            Element e = (Element) list.item(i);
            if ("firebase_messaging_auto_init_enabled".equals(e.getAttributeNS(ANDROID_NS, "name"))) {
                assertEquals("application", e.getParentNode().getNodeName());
                autoInit = e.getAttributeNS(ANDROID_NS, "value");
            }
        }
        assertEquals("false", autoInit);
        String plugin = SourceText.withoutComments(SourceText.read(
                "../../node_modules/@capacitor/push-notifications/android/src/main/java/"
                        + "com/capacitorjs/plugins/pushnotifications/PushNotificationsPlugin.java"));
        assertTrue(SourceText.body(plugin, "public void register(")
                .contains("FirebaseMessaging.getInstance().setAutoInitEnabled(true)"));
    }

    /** A permission implies a required hardware feature unless the feature is
     *  declared: each one the app can do without is declared optional, so Play
     *  offers the app to devices without it. */
    @Test
    public void theHardwareTheAppCanDoWithoutIsOptional() throws Exception {
        NodeList list = xml("src/main/AndroidManifest.xml").getElementsByTagName("uses-feature");
        Set<String> optional = new TreeSet<>();
        for (int i = 0; i < list.getLength(); i++) {
            Element e = (Element) list.item(i);
            assertEquals("every declared feature is optional", "false", e.getAttributeNS(ANDROID_NS, "required"));
            optional.add(e.getAttributeNS(ANDROID_NS, "name"));
        }
        assertEquals(new TreeSet<>(java.util.Arrays.asList(
                "android.hardware.camera", "android.hardware.microphone", "android.hardware.location")),
                optional);
    }

    /** Capacitor's file chooser resolves ACTION_IMAGE_CAPTURE before it opens
     *  the camera; on API 30+ that needs the intent in <queries>. */
    @Test
    public void theCameraAppIsVisibleToTheFileChooser() throws Exception {
        NodeList actions = xml("src/main/AndroidManifest.xml").getElementsByTagName("action");
        boolean found = false;
        for (int i = 0; i < actions.getLength(); i++) {
            Element a = (Element) actions.item(i);
            if ("android.media.action.IMAGE_CAPTURE".equals(a.getAttributeNS(ANDROID_NS, "name"))
                    && "intent".equals(a.getParentNode().getNodeName())
                    && "queries".equals(a.getParentNode().getParentNode().getNodeName())) {
                found = true;
            }
        }
        assertTrue(found);
    }

    /** The app asks for no battery exemption itself; the alarm's settings are
     *  each explained once, at the first dashboard of a process, never as a
     *  settings screen opened unprompted at start. */
    @Test
    public void theAlarmSettingsAreOfferedOnceAndNeverRequestedDirectly() throws Exception {
        assertFalse(permissions().contains("android.permission.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS"));
        String main = SourceText.javaSource("MainActivity");
        assertFalse(main.contains("ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS"));
        assertFalse("nothing asked from onCreate",
                SourceText.body(main, "protected void onCreate(").contains("Offer"));
        assertTrue(SourceText.body(main, "private void dashboardReady(")
                .contains("runOnUiThread(this::askStartupQuestionsOnce)"));
        String ask = SourceText.body(main, "private void askStartupQuestionsOnce(");
        assertTrue(ask.contains("if (sStartupQuestionsAsked"));
        assertTrue("never over the setup page", ask.contains("installStore.active() == null"));
        assertTrue("one question per start", ask.contains("if (offerFullScreenIntentOnce()) return;"));
        String once = SourceText.body(main, "private boolean offerOnce(");
        int read = once.indexOf("prefs.getBoolean(key, false)");
        int write = once.indexOf("putBoolean(key, true)");
        assertTrue("recorded when offered, so it is asked once", read >= 0 && write > read);
        String fsi = SourceText.body(main, "private boolean offerFullScreenIntentOnce(");
        assertTrue(fsi.contains("canUseFullScreenIntent()"));
        assertTrue(fsi.contains("offerOnce(FULL_SCREEN_OFFERED)"));
        String battery = SourceText.body(main, "private boolean offerBatterySettingsOnce(");
        assertTrue(battery.contains("isIgnoringBatteryOptimizations("));
        assertTrue(battery.contains("isBackgroundRestricted()"));
        assertTrue(battery.contains("offerOnce(BATTERY_SETTINGS_OFFERED)"));
        assertTrue(battery.contains("ACTION_APPLICATION_DETAILS_SETTINGS"));
    }

    /** A danger message Android downgraded (or a refused start) still raises
     *  the alarm: a full-screen notification whose activity starts the service. */
    @Test
    public void aDangerMessageThatCannotStartTheServiceStillRaisesTheAlarm() throws Exception {
        String service = SourceText.javaSource("NotificationService");
        String received = SourceText.body(service, "public void onMessageReceived(");
        assertTrue(received.contains("remoteMessage.getPriority() == RemoteMessage.PRIORITY_HIGH"));
        assertTrue(received.contains("catch (IllegalStateException e)"));
        assertTrue(received.contains("showLockScreenAlarm(alarmIntent)"));
        String fallback = SourceText.body(service, "private void showLockScreenAlarm(");
        assertTrue(fallback.contains("new NotificationCompat.Builder(this, \"notif_danger\")"));
        assertTrue(fallback.contains(".setFullScreenIntent(pending, true)"));
        assertTrue(fallback.contains("AlarmActivity.EXTRA_START_ALARM, true"));
        assertTrue("the service's notification replaces it",
                fallback.contains("nm.notify(AlarmService.NOTIFICATION_ID"));
        String activity = SourceText.javaSource("AlarmActivity");
        String resume = SourceText.body(activity, "protected void onResume(");
        assertTrue(resume.contains("startForegroundService(start)"));
        assertTrue(SourceText.body(activity, "protected void onCreate(")
                .contains("savedInstanceState == null"));
    }

    @Test
    public void theFileProviderSharesOnlyTheCameraCaptureDirectory() throws Exception {
        Element paths = xml("src/main/res/xml/file_paths.xml").getDocumentElement();
        NodeList children = paths.getChildNodes();
        int entries = 0;
        for (int i = 0; i < children.getLength(); i++) {
            Node n = children.item(i);
            if (n.getNodeType() != Node.ELEMENT_NODE) continue;
            entries++;
            assertEquals("external-files-path", n.getNodeName());
            assertEquals("Pictures/", ((Element) n).getAttribute("path"));
        }
        assertEquals(1, entries);
    }

    @Test
    public void theReleaseBuildIsSignedAndNotDebuggable() throws Exception {
        String gradle = SourceText.withoutComments(SourceText.read("build.gradle"));
        String release = SourceText.body(gradle, "buildTypes");
        assertTrue(release.contains("signingConfig signingConfigs.release"));
        assertTrue(release.contains("debuggable false"));
        String signing = SourceText.body(gradle, "signingConfigs");
        assertTrue("the fields are set only when every value exists",
                signing.contains("if (otodockReleaseSigningReady)"));
        assertTrue(gradle.contains("Release signing is not configured"));
        String guarded = gradle.substring(gradle.indexOf("otodockReleasePackagingTasks ="));
        guarded = guarded.substring(0, guarded.indexOf("as Set"));
        for (String task : new String[]{"assembleRelease", "bundleRelease", "signReleaseBundle",
                "packageRelease", "packageReleaseBundle", "installRelease"}) {
            assertTrue(task + " is guarded", guarded.contains("'" + task + "'"));
        }
        assertFalse("no keystore in the repo", new File("release.keystore").exists());
    }

    /** A bundle built without the Firebase file would ship an app with no
     *  push: the store packaging tasks refuse without it. */
    @Test
    public void theStoreBundleRefusesToBuildWithoutTheFirebaseFile() throws Exception {
        String gradle = SourceText.withoutComments(SourceText.read("build.gradle"));
        assertTrue(gradle.contains("A store bundle needs app/google-services.json"));
        String guarded = gradle.substring(gradle.indexOf("otodockStoreTasks ="));
        guarded = guarded.substring(0, guarded.indexOf("as Set"));
        for (String task : new String[]{"bundleRelease", "signReleaseBundle", "packageReleaseBundle"}) {
            assertTrue(task + " is guarded", guarded.contains("'" + task + "'"));
        }
        assertTrue("missing or empty, as the plugin's own condition reads it",
                gradle.contains("otodockFirebaseFile.exists() && !otodockFirebaseFile.text.trim().isEmpty()"));
        assertTrue(gradle.contains("if (!otodockFirebaseConfigured && graph.allTasks.any {"));
    }

    /** The setup page's fonts ride in the app's assets (setup-fonts/, from the
     *  web build): no asset pattern may drop them. */
    @Test
    public void theAssetsKeepTheSetupPagesFonts() throws Exception {
        String gradle = SourceText.withoutComments(SourceText.read("build.gradle"));
        int at = gradle.indexOf("ignoreAssetsPattern");
        assertTrue(at >= 0);
        String pattern = gradle.substring(at, gradle.indexOf('\n', at));
        for (String kept : new String[]{"woff", "font", "setup"}) {
            assertFalse(kept, pattern.toLowerCase(java.util.Locale.ROOT).contains(kept));
        }
    }

    @Test
    public void notificationsCarryALockScreenVersionWithoutTheirText() throws Exception {
        String service = SourceText.javaSource("NotificationService");
        assertEquals(3, SourceText.count(service, ".setVisibility(NotificationCompat.VISIBILITY_PRIVATE)"));
        assertEquals(3, SourceText.count(service, ".setPublicVersion("));
        String alarm = SourceText.javaSource("AlarmService");
        assertEquals(1, SourceText.count(alarm, ".setVisibility(NotificationCompat.VISIBILITY_PRIVATE)"));
        assertEquals(1, SourceText.count(alarm, ".setPublicVersion("));
    }

    @Test
    public void capSyncWritesNoWildcardCordovaAccessRule() throws Exception {
        String config = SourceText.withoutComments(SourceText.read("../../capacitor.config.ts"));
        assertTrue(config.contains("accessOrigins: []"));
    }

    /** The system splash is the only splash: the Capacitor plugin returns
     *  before its own install when its duration is 0 (pinned in the config
     *  and in the plugin's source), and the activity installs the AndroidX
     *  one before its content, holds it for the first real screen on a
     *  launch the system shows a splash for, lets it go on the page's
     *  signal, the deadline or the bundled page, and tells the page when the
     *  splash is gone, or that it never came. The names the two halves share
     *  are read from the page's files. */
    @Test
    public void theSystemSplashIsHeldForTheFirstScreen() throws Exception {
        String config = SourceText.withoutComments(SourceText.read("../../capacitor.config.ts"));
        assertTrue(SourceText.body(config, "SplashScreen:").contains("launchShowDuration: 0"));
        String plugin = SourceText.withoutComments(SourceText.read(
                "../../node_modules/@capacitor/splash-screen/android/src/main/java/"
                        + "com/capacitorjs/plugins/splashscreen/SplashScreen.java"));
        assertTrue(SourceText.body(plugin, "public void showOnLaunch(")
                .trim().startsWith("if (config.getLaunchShowDuration() == 0) {"));
        String main = SourceText.javaSource("MainActivity");
        String onCreate = SourceText.body(main, "protected void onCreate(");
        int install = onCreate.indexOf("installBootSplash(savedInstanceState)");
        int sup = onCreate.indexOf("super.onCreate(");
        assertTrue("installed before the content", install >= 0 && sup > install);
        assertFalse(main.contains("showCustomSplash"));
        assertFalse(main.contains("R.drawable.splash"));
        String boot = SourceText.body(main, "private void installBootSplash(");
        assertTrue(boot.contains("SplashScreen.installSplashScreen(this)"));
        assertTrue("held on a launch the system shows a splash for",
                boot.contains("bootHeld = !sSwitchInProgress && (savedInstanceState == null || !sColdStarted);"));
        assertTrue("nothing installed on a recreate", boot.contains("if (!bootHeld) return;"));
        assertTrue("the hand-over is recorded first", boot.contains("splashLeaving = true;"));
        assertTrue("the deadline releases", boot.contains(">= BOOT_HOLD_MAX_MS) releaseBoot(\"deadline\")"));
        assertTrue("the deadline waits for a page", boot.contains("pageCommitted && held >= BOOT_HOLD_MAX_MS"));
        assertTrue("a WebView with no page releases at the cap",
                boot.contains("held >= BOOT_HOLD_CAP_MS) releaseBoot(\"deadline, no page\")"));
        assertTrue("a page committing after the deadline releases at once",
                SourceText.body(main, "public void onPageCommitVisible(").contains("releaseBoot(\"deadline\")"));
        int animate = boot.indexOf(".alpha(0f).setDuration(BOOT_FADE_MS).withEndAction(gone)");
        int cue = boot.indexOf("tellSplash(wv, \"leaving\")");
        assertTrue("the fade starts before the cue", animate >= 0 && cue > animate);
        assertTrue("the removal is guarded", boot.contains("view.postDelayed(gone, BOOT_FADE_MS * 4)"));
        int remove = boot.indexOf("provider.remove();");
        int bars = boot.indexOf("reapplyBars();", remove);
        assertTrue("the bars are put back after the removal", remove >= 0 && bars > remove);
        String release = SourceText.body(main, "private void releaseBoot(");
        assertTrue(release.contains("bootHeld = false;") && release.contains("postInvalidate()"));
        assertTrue("a splash the system never hands over is reported gone",
                release.contains("if (splashLeaving || splashGone) return;") && release.contains("tellSplashGone();"));
        assertTrue("the page is told only on the active install's dashboard",
                SourceText.body(main, "private void tellSplash(").trim().startsWith("if (!showsDashboard(wv)) return;"));
        assertTrue(SourceText.body(main, "private void pushBarInsets(").contains("!showsDashboard(wv)) return;"));
        assertTrue(SourceText.body(main, "private boolean showsDashboard(")
                .contains("NativeBridgePolicy.sameOrigin(activeOrigin, url)"));
        String bootJs = SourceText.read("../../public/boot.js");
        assertTrue("the page's cue function", bootJs.contains("window.__otodockSplash = splash"));
        assertTrue(main.contains("window.__otodockSplash && window.__otodockSplash('"));
        for (String word : new String[]{"leaving", "gone"}) {
            assertTrue(word, main.contains("tellSplash(wv, \"" + word + "\")"));
            assertTrue(word, bootJs.contains("'" + word + "'"));
        }
        String shell = SourceText.read("../../index.html");
        for (String name : new String[]{"--oto-bar-top", "--oto-bar-bottom"}) {
            assertTrue(name, main.contains(name) && shell.contains(name));
        }
        assertTrue("an older app gets the field for its strips", bootJs.contains("m: 'setContainerColor', a: [FIELD]"));
        String first = SourceText.body(main, "private void firstScreen(");
        assertTrue("the signal waits for the WebView's frame, bounded",
                first.contains("postVisualStateCallback(") && first.contains("BOOT_VISUAL_WAIT_MS"));
        String loaded = SourceText.body(main, "public void onPageLoaded(");
        assertTrue("the bundled page releases itself on the phone's theme",
                loaded.contains("paintSetupPage();") && loaded.contains("releaseBoot(\"setup\")"));
        assertTrue("the startup questions wait for the release",
                SourceText.body(main, "private void askStartupQuestionsOnce(").contains("if (bootHeld) {"));
        for (File dir : new File("src/main/res").listFiles()) {
            assertFalse(dir.getName(), dir.getName().startsWith("drawable") && new File(dir, "splash.png").exists());
        }
    }

    /** What shows before the page paints, and the bars over it, is the boot
     *  field from the tokens; the plugins' load-time values are the same;
     *  the page's colour sets both bars and survives a configuration change;
     *  every small icon is the notification mark. */
    @Test
    public void theBootFieldIsTheTokensAndTheIconsAreTheMark() throws Exception {
        String colors = SourceText.read("src/main/res/values/colors.xml");
        Matcher field = Pattern.compile("<color name=\"bootField\">(#[0-9a-f]{6})</color>").matcher(colors);
        assertTrue(field.find());
        String config = SourceText.withoutComments(SourceText.read("../../capacitor.config.ts"));
        assertTrue(SourceText.body(config, "StatusBar:").contains("backgroundColor: '" + field.group(1) + "'"));
        assertTrue(SourceText.body(config, "StatusBar:").contains("style: 'DARK'"));
        assertTrue(SourceText.body(config, "SystemBars:").contains("style: 'DARK'"));
        assertFalse(config.contains("androidSplashResourceName"));
        String styles = SourceText.read("src/main/res/values/styles.xml");
        assertTrue(styles.contains("<item name=\"windowSplashScreenBackground\">@color/bootField</item>"));
        assertTrue(styles.contains("<item name=\"windowSplashScreenAnimatedIcon\">@drawable/ic_launcher_foreground</item>"));
        // No icon background: every level then draws the icon at 288 dp,
        // and the page's boot screen draws the mark at that size (the mark
        // is 47.7 dp wide on the 108 dp canvas, times 288 / 108).
        assertFalse(styles.contains("windowSplashScreenIconBackgroundColor"));
        NodeList styleNodes = xml("src/main/res/values/styles.xml").getElementsByTagName("style");
        Set<String> activityBars = new TreeSet<>();
        Set<String> launch = new TreeSet<>();
        String launchParent = null;
        for (int i = 0; i < styleNodes.getLength(); i++) {
            Element style = (Element) styleNodes.item(i);
            Set<String> into;
            if ("AppTheme.NoActionBar".equals(style.getAttribute("name"))) into = activityBars;
            else if ("AppTheme.NoActionBarLaunch".equals(style.getAttribute("name"))) {
                into = launch;
                launchParent = style.getAttribute("parent");
            } else continue;
            NodeList items = style.getElementsByTagName("item");
            for (int j = 0; j < items.getLength(); j++) {
                Element item = (Element) items.item(j);
                into.add(item.getAttribute("name") + "=" + item.getTextContent().trim());
            }
        }
        assertEquals("Theme.SplashScreen", launchParent);
        assertTrue("a notification tap or a relaunch shows the icon too",
                launch.contains("android:windowSplashScreenBehavior=icon_preferred"));
        assertTrue(SourceText.read("../../index.html").contains("--boot-mark: 127px"));
        assertTrue("the activity theme carries the boot bars too", activityBars.containsAll(java.util.Arrays.asList(
                "android:statusBarColor=@color/bootField", "android:navigationBarColor=@color/bootFieldDeep",
                "android:windowLightStatusBar=false", "android:windowLightNavigationBar=false")));
        String main = SourceText.javaSource("MainActivity");
        String onCreate = SourceText.body(main, "protected void onCreate(");
        assertTrue(onCreate.contains("getColor(R.color.bootField)") && onCreate.contains("getColor(R.color.bootFieldDeep)"));
        assertTrue(onCreate.contains("paintBars(getColor(R.color.bootField), getColor(R.color.bootFieldDeep), false)"));
        String set = SourceText.body(main, "private void setContainerColor(int to)");
        assertTrue(set.contains("paintBars(to, to, ColorUtils.calculateLuminance(to) > 0.5)"));
        assertTrue("the field from an older page's boot is already painted",
                set.trim().startsWith("if (to == getColor(R.color.bootField)) return;"));
        String changed = SourceText.body(main, "public void onConfigurationChanged(");
        int sup = changed.indexOf("super.onConfigurationChanged(newConfig);");
        int bars = changed.indexOf("reapplyBars();");
        assertTrue("re-applied after the plugins", sup >= 0 && bars > sup);
        for (String cls : new String[]{"NotificationService", "AlarmService"}) {
            String src = SourceText.javaSource(cls);
            assertEquals(cls, 0, SourceText.count(src, "setSmallIcon(R.mipmap.ic_launcher)"));
            assertTrue(cls, SourceText.count(src, "setSmallIcon(R.drawable.ic_stat)") > 0);
        }
        NodeList meta = xml("src/main/AndroidManifest.xml").getElementsByTagName("meta-data");
        String icon = null;
        for (int i = 0; i < meta.getLength(); i++) {
            Element e = (Element) meta.item(i);
            if ("com.google.firebase.messaging.default_notification_icon".equals(e.getAttributeNS(ANDROID_NS, "name"))) {
                icon = e.getAttributeNS(ANDROID_NS, "resource");
            }
        }
        assertEquals("@drawable/ic_stat", icon);
    }

    /** The WebView is padded for the keyboard ONCE, by the activity's own
     *  insets listener. Capacitor's SystemBars plugin pads the window's decor
     *  for it too unless its insets handling is off, and the plugin reads
     *  that only from the config a server boot hands it: both halves, and the
     *  plugin's own opt-out, are pinned, so a second keyboard padding cannot
     *  come back unseen. */
    @Test
    public void theKeyboardPadsTheWebViewOnce() throws Exception {
        String config = SourceText.withoutComments(SourceText.read("../../capacitor.config.ts"));
        String systemBars = SourceText.body(config, "SystemBars:");
        assertTrue(systemBars.contains("insetsHandling: 'disable'"));
        String main = SourceText.javaSource("MainActivity");
        assertEquals("one insets listener", 1, SourceText.count(main, "setOnApplyWindowInsetsListener("));
        assertEquals("one keyboard term", 1, SourceText.count(main, "Math.max(ime.bottom, bars.bottom)"));
        assertTrue(SourceText.body(main, "protected void load()").contains(".setPluginsConfiguration(pluginsConfiguration())"));
        assertTrue(SourceText.body(main, "private JSONObject pluginsConfiguration(").contains("getAssets().open(\"capacitor.config.json\")"));
        String plugin = SourceText.withoutComments(SourceText.read(
                "../../node_modules/@capacitor/android/capacitor/src/main/java/com/getcapacitor/plugin/SystemBars.java"));
        String listener = SourceText.body(plugin, "private void initWindowInsetsListener(");
        int optOut = listener.indexOf("INSETS_HANDLING_DISABLE.equals(insetsHandling)");
        int register = listener.indexOf("setOnApplyWindowInsetsListener(");
        assertTrue("the plugin's opt-out comes before its listener", optOut >= 0 && register > optOut);
        assertTrue(SourceText.body(plugin, "private void initSystemBars(").contains("getConfig().getString(\"insetsHandling\""));
    }
}
