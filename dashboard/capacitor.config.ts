import type { CapacitorConfig } from '@capacitor/cli'

const config: CapacitorConfig = {
  appId: 'com.otodock',
  appName: 'OtoDock',
  webDir: 'dist',

  // Server URL is set dynamically in MainActivity.load() from SharedPreferences.
  // On first launch (no URL saved), the local setup.html page is loaded so the
  // user can enter their server URL. allowNavigation is also set dynamically, to
  // the server's exact host (no wildcard).
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
  },
}

export default config
