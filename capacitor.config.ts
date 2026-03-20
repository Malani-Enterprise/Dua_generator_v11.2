import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'ai.mydua.app',
  appName: 'MyDua.AI',
  webDir: 'static',  // matches your existing static/ folder structure

  // ─── Server Configuration ───
  // In production, the app loads local files from static/.
  // The API calls need to reach your backend server.
  server: {
    // DEVELOPMENT: uncomment to load from live dev server
    // url: 'http://192.168.1.XXX:8000',
    // cleartext: true,

    // PRODUCTION: API requests route to your deployed backend.
    // The frontend JS will use the native bridge helper (see src/native-bridge.js)
    // to prepend the correct API_BASE_URL to all fetch calls.
    androidScheme: 'https',
    iosScheme: 'https',
    allowNavigation: [
      'mydua.ai',
      '*.mydua.ai',
      'api.stripe.com',       // Stripe payment
      'js.stripe.com',        // Stripe JS
      'cdnjs.cloudflare.com', // html2pdf.js, other CDN deps
    ],
  },

  // ─── Plugins ───
  plugins: {
    SplashScreen: {
      launchShowDuration: 2000,
      launchAutoHide: true,
      backgroundColor: '#0a0a0f',  // matches your dark theme
      showSpinner: false,
      // Use a custom splash image (gold calligraphy on dark bg)
      // Place at: ios/App/App/Assets.xcassets/Splash.imageset/
      //           android/app/src/main/res/drawable/splash.png
    },

    StatusBar: {
      style: 'DARK',             // light text on dark background
      backgroundColor: '#0a0a0f', // matches app background
    },

    PushNotifications: {
      // Daily check-in reminders, prayer request notifications
      presentationOptions: ['badge', 'sound', 'alert'],
    },

    Haptics: {
      // Used for emotion card selection feedback
    },

    Geolocation: {
      // Hajj/Umrah phase detection at sacred sites
    },

    Preferences: {
      // Replaces sessionStorage for persistent form state
      // Also stores: user token, emotion history, journey data
    },

    Network: {
      // Replaces browser online/offline events
      // More reliable on mobile, especially at Hajj sites
    },

    Filesystem: {
      // PDF journal export, cached du'a audio files
    },
  },

  // ─── iOS Specific ───
  ios: {
    contentInset: 'always',
    scrollEnabled: true,
    // Background audio for devotional du'a playback during Tawaf
    backgroundColor: '#0a0a0f',
    preferredContentMode: 'mobile',
  },

  // ─── Android Specific ───
  android: {
    backgroundColor: '#0a0a0f',
    allowMixedContent: false,
    // Keeps WebView alive when app is backgrounded (for audio)
    webContentsDebuggingEnabled: false, // set true for dev
  },
};

export default config;
