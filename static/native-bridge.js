/**
 * MyDua.AI — Native Bridge
 * =========================
 * Drop-in module that wraps Capacitor plugins into a clean API.
 * Your existing vanilla JS calls these helpers instead of raw browser APIs.
 *
 * USAGE: Add <script src="/native-bridge.js"></script> BEFORE your main <script> in index.html.
 *        Then use `MyDuaNative.*` anywhere in your existing code.
 *
 * DESIGN: Every function gracefully degrades to web APIs when Capacitor is not present
 *         (i.e., when the app runs in a regular browser). This means the same codebase
 *         works as both a website and a native app.
 */

const MyDuaNative = (() => {

  // ─── Platform Detection ───
  const isCapacitor = () => typeof window.Capacitor !== 'undefined' && window.Capacitor.isNativePlatform();
  const getPlatform = () => isCapacitor() ? window.Capacitor.getPlatform() : 'web'; // 'ios' | 'android' | 'web'

  // ─── API Base URL ───
  // In Capacitor, the app loads from local files, so API calls need an absolute URL.
  // In the browser, relative URLs work fine.
  let _apiBaseUrl = '';

  function setApiBaseUrl(url) {
    _apiBaseUrl = url.replace(/\/$/, ''); // strip trailing slash
  }

  function getApiBaseUrl() {
    return _apiBaseUrl;
  }

  /**
   * Wraps fetch() to prepend the API base URL when running in Capacitor.
   * Drop-in replacement for `fetch(url, options)` in your existing code.
   *
   * MIGRATION: Replace `fetch("/api/generate-dua-stream", opts)`
   *       with `MyDuaNative.apiFetch("/api/generate-dua-stream", opts)`
   */
  async function apiFetch(path, options = {}) {
    const url = path.startsWith('http') ? path : `${_apiBaseUrl}${path}`;
    return fetch(url, options);
  }

  // ─── Persistent Storage (replaces sessionStorage for form auto-save) ───
  // Capacitor Preferences survives app restarts; sessionStorage does not.

  async function storageSet(key, value) {
    if (isCapacitor()) {
      const { Preferences } = await import('@capacitor/preferences');
      await Preferences.set({ key, value: JSON.stringify(value) });
    } else {
      sessionStorage.setItem(key, JSON.stringify(value));
    }
  }

  async function storageGet(key) {
    if (isCapacitor()) {
      const { Preferences } = await import('@capacitor/preferences');
      const { value } = await Preferences.get({ key });
      return value ? JSON.parse(value) : null;
    } else {
      const raw = sessionStorage.getItem(key);
      return raw ? JSON.parse(raw) : null;
    }
  }

  async function storageRemove(key) {
    if (isCapacitor()) {
      const { Preferences } = await import('@capacitor/preferences');
      await Preferences.remove({ key });
    } else {
      sessionStorage.removeItem(key);
    }
  }

  // ─── Network Status (replaces window online/offline events) ───

  let _networkListeners = [];

  async function initNetwork() {
    if (isCapacitor()) {
      const { Network } = await import('@capacitor/network');

      // Get initial status
      const status = await Network.getStatus();
      _updateNetworkUI(status.connected);

      // Listen for changes
      Network.addListener('networkStatusChange', (status) => {
        _updateNetworkUI(status.connected);
        _networkListeners.forEach((fn) => fn(status.connected));
      });
    } else {
      // Fall back to browser events (existing behavior)
      window.addEventListener('online', () => {
        _updateNetworkUI(true);
        _networkListeners.forEach((fn) => fn(true));
      });
      window.addEventListener('offline', () => {
        _updateNetworkUI(false);
        _networkListeners.forEach((fn) => fn(false));
      });
    }
  }

  function onNetworkChange(callback) {
    _networkListeners.push(callback);
  }

  function _updateNetworkUI(isOnline) {
    const btn = document.getElementById('generateBtn');
    if (btn) btn.disabled = !isOnline;

    if (!isOnline) {
      // Use your existing showError() if available
      if (typeof showError === 'function') {
        showError('You appear to be offline. Please check your internet connection.');
      }
    } else {
      if (typeof hideError === 'function') {
        hideError();
      }
    }
  }

  // ─── Haptic Feedback (emotion card selection, button presses) ───

  async function hapticLight() {
    if (isCapacitor()) {
      const { Haptics, ImpactStyle } = await import('@capacitor/haptics');
      await Haptics.impact({ style: ImpactStyle.Light });
    }
    // No-op on web
  }

  async function hapticMedium() {
    if (isCapacitor()) {
      const { Haptics, ImpactStyle } = await import('@capacitor/haptics');
      await Haptics.impact({ style: ImpactStyle.Medium });
    }
  }

  async function hapticHeavy() {
    if (isCapacitor()) {
      const { Haptics, ImpactStyle } = await import('@capacitor/haptics');
      await Haptics.impact({ style: ImpactStyle.Heavy });
    }
  }

  async function hapticSelection() {
    if (isCapacitor()) {
      const { Haptics } = await import('@capacitor/haptics');
      await Haptics.selectionChanged();
    }
  }

  // ─── Share (native share sheet replaces WhatsApp/FB/SMS links) ───

  async function shareNative({ title, text, url }) {
    if (isCapacitor()) {
      const { Share } = await import('@capacitor/share');
      try {
        await Share.share({ title, text, url, dialogTitle: 'Share this du\'a' });
        return { shared: true };
      } catch (err) {
        // User cancelled
        return { shared: false };
      }
    } else if (navigator.share) {
      // Web Share API fallback
      try {
        await navigator.share({ title, text, url });
        return { shared: true };
      } catch (err) {
        return { shared: false };
      }
    } else {
      // Final fallback: copy to clipboard
      await navigator.clipboard.writeText(url || text);
      return { shared: false, copied: true };
    }
  }

  // ─── Push Notifications (daily check-in reminders) ───

  async function initPushNotifications() {
    if (!isCapacitor()) return { granted: false, reason: 'not-native' };

    const { PushNotifications } = await import('@capacitor/push-notifications');

    // Request permission
    const permResult = await PushNotifications.requestPermissions();
    if (permResult.receive !== 'granted') {
      return { granted: false, reason: 'denied' };
    }

    // Register with APNS (iOS) / FCM (Android)
    await PushNotifications.register();

    // Listen for registration token (send to your backend for push delivery)
    PushNotifications.addListener('registration', (token) => {
      console.log('[Push] Registration token:', token.value);
      // TODO: POST token.value to your backend /api/push/register
      // apiFetch('/api/push/register', {
      //   method: 'POST',
      //   headers: { 'Content-Type': 'application/json' },
      //   body: JSON.stringify({ token: token.value, platform: getPlatform() })
      // });
    });

    // Listen for incoming notifications when app is in foreground
    PushNotifications.addListener('pushNotificationReceived', (notification) => {
      console.log('[Push] Received in foreground:', notification);
      // Could show an in-app banner: "How is your heart today?"
    });

    // Listen for notification tap (app was in background)
    PushNotifications.addListener('pushNotificationActionPerformed', (action) => {
      console.log('[Push] Tapped:', action);
      const url = action.notification?.data?.url;
      if (url) {
        window.location.href = url;
      }
    });

    return { granted: true };
  }

  // ─── Geolocation (Hajj/Umrah phase detection) ───

  // Sacred site coordinates with approximate geofence radius (meters)
  const SACRED_SITES = {
    kaaba:       { lat: 21.4225,  lng: 39.8262,  radius: 500,  name: 'Masjid al-Haram (Ka\'bah)' },
    safa_marwa:  { lat: 21.4234,  lng: 39.8267,  radius: 300,  name: 'Safa & Marwa' },
    arafat:      { lat: 21.3549,  lng: 39.9842,  radius: 3000, name: 'Mount Arafat' },
    muzdalifah:  { lat: 21.3833,  lng: 39.9333,  radius: 2000, name: 'Muzdalifah' },
    mina:        { lat: 21.4133,  lng: 39.8933,  radius: 2000, name: 'Mina' },
    masjid_nabawi: { lat: 24.4672, lng: 39.6112, radius: 500,  name: 'Masjid an-Nabawi (Madinah)' },
  };

  async function getCurrentLocation() {
    if (isCapacitor()) {
      const { Geolocation } = await import('@capacitor/geolocation');
      try {
        const pos = await Geolocation.getCurrentPosition({
          enableHighAccuracy: true,
          timeout: 10000,
        });
        return { lat: pos.coords.latitude, lng: pos.coords.longitude, accuracy: pos.coords.accuracy };
      } catch (err) {
        console.warn('[Geo] Location error:', err.message);
        return null;
      }
    } else if ('geolocation' in navigator) {
      return new Promise((resolve) => {
        navigator.geolocation.getCurrentPosition(
          (pos) => resolve({ lat: pos.coords.latitude, lng: pos.coords.longitude, accuracy: pos.coords.accuracy }),
          () => resolve(null),
          { enableHighAccuracy: true, timeout: 10000 }
        );
      });
    }
    return null;
  }

  /**
   * Detect which sacred site the user is near (if any).
   * Returns the site key and name, or null if not near any known site.
   */
  async function detectSacredSite() {
    const loc = await getCurrentLocation();
    if (!loc) return null;

    for (const [key, site] of Object.entries(SACRED_SITES)) {
      const distance = _haversineDistance(loc.lat, loc.lng, site.lat, site.lng);
      if (distance <= site.radius) {
        return { key, name: site.name, distance: Math.round(distance) };
      }
    }
    return null;
  }

  // Haversine formula for distance between two GPS coordinates (meters)
  function _haversineDistance(lat1, lng1, lat2, lng2) {
    const R = 6371000; // Earth radius in meters
    const dLat = (lat2 - lat1) * Math.PI / 180;
    const dLng = (lng2 - lng1) * Math.PI / 180;
    const a = Math.sin(dLat / 2) ** 2 +
              Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
              Math.sin(dLng / 2) ** 2;
    return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }

  // ─── Audio Playback (background audio for devotional du'as) ───

  let _audioElement = null;

  /**
   * Play a du'a audio file. Supports background playback on native.
   * @param {string} audioUrl - URL to the audio file (from /api/tts or cached)
   * @param {object} metadata - { title, artist } for lock screen display
   */
  async function playAudio(audioUrl, metadata = {}) {
    const url = audioUrl.startsWith('http') ? audioUrl : `${_apiBaseUrl}${audioUrl}`;

    // Clean up previous audio
    if (_audioElement) {
      _audioElement.pause();
      _audioElement = null;
    }

    _audioElement = new Audio(url);

    // Set media session metadata for lock screen controls
    if ('mediaSession' in navigator) {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: metadata.title || 'Your Du\'a',
        artist: metadata.artist || 'MyDua.AI',
        album: metadata.album || 'Personal Supplications',
        // artwork: [{ src: '/icons/icon-512x512.png', sizes: '512x512', type: 'image/png' }],
      });

      navigator.mediaSession.setActionHandler('play', () => _audioElement?.play());
      navigator.mediaSession.setActionHandler('pause', () => _audioElement?.pause());
    }

    await _audioElement.play();
    return _audioElement;
  }

  function pauseAudio() {
    if (_audioElement) _audioElement.pause();
  }

  function resumeAudio() {
    if (_audioElement) _audioElement.play();
  }

  function getAudioState() {
    if (!_audioElement) return { playing: false, currentTime: 0, duration: 0 };
    return {
      playing: !_audioElement.paused,
      currentTime: _audioElement.currentTime,
      duration: _audioElement.duration || 0,
    };
  }

  // ─── Status Bar (dark theme) ───

  async function initStatusBar() {
    if (!isCapacitor()) return;

    const { StatusBar, Style } = await import('@capacitor/status-bar');
    await StatusBar.setStyle({ style: Style.Dark });

    if (getPlatform() === 'android') {
      await StatusBar.setBackgroundColor({ color: '#0a0a0f' });
    }
  }

  // ─── Splash Screen ───

  async function hideSplash() {
    if (!isCapacitor()) return;
    const { SplashScreen } = await import('@capacitor/splash-screen');
    await SplashScreen.hide();
  }

  // ─── File System (save PDF journal to device) ───

  /**
   * Save a blob (PDF, audio) to the device's Documents directory.
   * Returns the saved file URI for sharing.
   */
  async function saveToDevice(blob, filename) {
    if (isCapacitor()) {
      const { Filesystem, Directory } = await import('@capacitor/filesystem');

      // Convert blob to base64
      const reader = new FileReader();
      const base64 = await new Promise((resolve) => {
        reader.onloadend = () => resolve(reader.result.split(',')[1]);
        reader.readAsDataURL(blob);
      });

      const result = await Filesystem.writeFile({
        path: filename,
        data: base64,
        directory: Directory.Documents,
      });

      return result.uri;
    } else {
      // Web fallback: trigger browser download
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
      return null;
    }
  }

  // ─── Deep Links (prayer request inbox: mydua.ai/journey/{code}) ───

  async function initDeepLinks(callback) {
    if (!isCapacitor()) return;

    const { App } = await import('@capacitor/app');
    App.addListener('appUrlOpen', (event) => {
      // event.url = "https://mydua.ai/journey/abc123"
      const url = new URL(event.url);
      if (callback) callback(url.pathname, url.searchParams);
    });
  }

  // ─── App Lifecycle ───

  async function initAppLifecycle() {
    if (!isCapacitor()) return;

    const { App } = await import('@capacitor/app');

    // Handle back button on Android
    App.addListener('backButton', ({ canGoBack }) => {
      if (canGoBack) {
        window.history.back();
      } else {
        App.exitApp();
      }
    });

    // Save form state when app goes to background
    App.addListener('appStateChange', ({ isActive }) => {
      if (!isActive) {
        // Trigger your existing saveFormState() function
        if (typeof saveFormState === 'function') {
          saveFormState();
        }
      }
    });
  }

  // ─── Initialization (call once on app startup) ───

  async function init(apiBaseUrl) {
    if (apiBaseUrl) setApiBaseUrl(apiBaseUrl);

    await initNetwork();
    await initStatusBar();
    await initAppLifecycle();
    await initDeepLinks();

    // Register service worker (works in both web and Capacitor)
    if ('serviceWorker' in navigator) {
      try {
        await navigator.serviceWorker.register('/service-worker.js');
        console.log('[SW] Service worker registered');
      } catch (err) {
        console.warn('[SW] Service worker registration failed:', err);
      }
    }

    // Hide splash screen after app is ready
    await hideSplash();

    console.log(`[MyDuaNative] Initialized on platform: ${getPlatform()}`);
  }

  // ─── Public API ───
  return {
    // Platform
    isCapacitor,
    getPlatform,
    init,

    // API
    setApiBaseUrl,
    getApiBaseUrl,
    apiFetch,

    // Storage
    storageSet,
    storageGet,
    storageRemove,

    // Network
    initNetwork,
    onNetworkChange,

    // Haptics
    hapticLight,
    hapticMedium,
    hapticHeavy,
    hapticSelection,

    // Share
    shareNative,

    // Push
    initPushNotifications,

    // Location
    getCurrentLocation,
    detectSacredSite,
    SACRED_SITES,

    // Audio
    playAudio,
    pauseAudio,
    resumeAudio,
    getAudioState,

    // Files
    saveToDevice,

    // Deep Links
    initDeepLinks,
  };
})();

// Expose globally for use in existing vanilla JS
window.MyDuaNative = MyDuaNative;
