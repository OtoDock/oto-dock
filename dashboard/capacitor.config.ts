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

  plugins: {
    SplashScreen: {
      // Capacitor's splash plugin opts out — MainActivity shows a custom
      // full-bleed splash overlay so the OtoDock wordmark is visible (the
      // Android 12+ system splash clips drawables to a 192dp icon circle).
      launchAutoHide: true,
      launchShowDuration: 0,
      backgroundColor: '#0C5CA1',
      androidSplashResourceName: 'splash',
      showSpinner: false,
      splashFullScreen: false,
      splashImmersive: false,
    },
    StatusBar: {
      style: 'LIGHT',
      backgroundColor: '#FAF9F9',
    },
  },
}

export default config
