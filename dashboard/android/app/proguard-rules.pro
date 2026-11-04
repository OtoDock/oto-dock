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

# Line numbers in a crash's stack trace, under one file name.
-keepattributes SourceFile,LineNumberTable
-renamesourcefileattribute SourceFile
