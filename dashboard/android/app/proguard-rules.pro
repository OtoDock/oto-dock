# R8 rules for the release build (build.gradle). AGP's default
# proguard-android-optimize.txt already keeps every @JavascriptInterface
# method and the runtime annotations; Capacitor's own rules keep every
# plugin class and its @PluginMethod methods (NativeChannelPlugin and
# PushUnconfiguredPlugin among them); the manifest keeps the activities and
# services; Firebase and Play services ship their own rules. The native
# channel's method names are JSON strings, never reflection.

# The bundled setup page calls this raw interface by name
# (window.Android.addInstallation(...) and the rest): kept explicitly, so a
# change to the default file cannot rename them.
-keepclassmembers class com.otodock.MainActivity$SetupBridge {
    @android.webkit.JavascriptInterface public <methods>;
}

# Android's WebView offers full screen to a page only when the
# WebChromeClient's class declares both of these: it looks for them by name
# (its doesSupportFullscreen). R8 removes a method that only calls its
# parent, which is all Capacitor's onHideCustomView does, and without the
# pair no video goes full screen, the dashboard's player or a frame's own.
# The build's checkReleaseFullscreen reads the shrunk dex for both.
-keepclassmembers class * extends android.webkit.WebChromeClient {
    public void onShowCustomView(android.view.View, android.webkit.WebChromeClient$CustomViewCallback);
    public void onHideCustomView();
}

# Line numbers in a crash's stack trace, under one file name.
-keepattributes SourceFile,LineNumberTable
-renamesourcefileattribute SourceFile
