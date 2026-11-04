import type { CapacitorConfig } from '@capacitor/cli'

const config: CapacitorConfig = {
  appId: 'com.otodock',
  appName: 'OtoDock',
  webDir: 'dist',

  // The server URL is set at run time by each shell (Android's
  // MainActivity.load() from SharedPreferences, the iOS ShellViewController
  // from its installations store). On first launch (no server saved), the
  // bundled setup.html page is loaded so the person can enter their server
  // address. allowNavigation is also set at run time, to the server's exact
  // host (no wildcard).
  server: {
    cleartext: false,
    allowNavigation: [],
  },

  android: {
    // Allow mixed content if needed (e.g., HTTP resources on HTTPS page)
    allowMixedContent: false,
    // Use Chrome-based WebView
    webContentsDebuggingEnabled: false,
  },

  ios: {
    // The shell sets the web view's frame itself (ShellViewController): the
    // scroll view adds no inset of its own.
    contentInset: 'never',
    allowsLinkPreview: false,
    // The boot field, the web view's own background between two documents.
    backgroundColor: '#1d4f80',
    // The notification center's delegate is the app's own (PushBridge), set
    // before the launch ends: a bridge must not take it at every switch.
    handleApplicationNotifications: false,
    // Only the plugins the iOS shell uses. The status bar plugin resizes the
    // web view to the window on every rotation and holds the bridge; the
    // splash plugin draws a launch view of its own: the shell owns both.
    includePlugins: [
      '@capacitor/browser',
      '@capacitor/geolocation',
      '@capacitor/push-notifications',
      '@capacitor-community/text-to-speech',
      '@capgo/capacitor-speech-recognition',
    ],
  },

  // cap sync writes res/xml/config.xml from this; unset, it emits a wildcard
  // <access origin="*">. No Cordova plugin reads it; keep it empty anyway.
  cordova: {
    accessOrigins: [],
  },

  // MainActivity.load() reads this block from the synced capacitor.config.json
  // for a server boot (Capacitor reads it itself for the setup page), so the
  // plugins see the same values on both.
  plugins: {
    // The app's own insets listener (MainActivity) pads the WebView for the
    // system bars and the keyboard. Capacitor's SystemBars plugin must not pad
    // the window a second time: 8.5.2 moved its listener from the WebView's
    // parent, where the app's replaced it, to the window's decor, where both
    // ran and a band the keyboard's height sat above the keyboard.
    SplashScreen: {
      // Capacitor's splash plugin opts out (a zero duration installs no
      // splash of its own): MainActivity installs the system splash itself
      // and holds it until the dashboard's first real screen.
      launchAutoHide: true,
      launchShowDuration: 0,
      showSpinner: false,
      splashFullScreen: false,
      splashImmersive: false,
    },
    // The bars over the boot field until the dashboard's theme arrives: light
    // icons, and the status bar on the field's colour (colors.xml's
    // bootField; a test holds the two equal). MainActivity owns the bars from
    // the page's colour after that.
    SystemBars: {
      insetsHandling: 'disable',
      style: 'DARK',
    },
    StatusBar: {
      style: 'DARK',
      backgroundColor: '#1d4f80',
    },
    // iOS: what a push shows while the app is in front. A push arrives only
    // for another installation, or while the socket was down (the active
    // installation's notifications come as socket toasts), so it shows.
    PushNotifications: {
      presentationOptions: ['banner', 'list', 'sound'],
    },
  },
}

export default config
