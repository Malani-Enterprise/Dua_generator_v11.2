# Changelog

## v1.5.6 — Third-Party Audit Patch (March 2026)

Security and reliability fixes for three Medium-severity findings from an independent code review of v1.5.5.

### F-03: Admin Dashboard — GET → POST Auth with Session Cookie
- **Before:** Admin password was sent as `?pw=` query parameter via `GET` form. This exposed the password in URL bars, browser history, server access logs, and proxy logs.
- **After:** Login form now uses `method="POST"`. On successful auth, a signed `admin_session` cookie is set (`httponly`, `secure` in production, `samesite=strict`, 1-hour expiry). Subsequent GET requests to `/admin/stats` validate the cookie — no password in the URL at any point.
- Added `Cache-Control: no-store` on all admin pages to prevent caching of sensitive dashboard data.
- Added `/admin/logout` endpoint that clears the session cookie and redirects to login.
- Cookie token is HMAC-SHA256 signed using `SECRET_KEY` — not guessable without the server secret.

### F-04: `/api/email-subscribe` Endpoint Implemented
- **Before:** The frontend `submitPostGenEmail()` function called `POST /api/email-subscribe` after emailing a du'a to also subscribe the user to the mailing list. But no such route existed in `app.py` — the call silently returned a `405 Method Not Allowed` error.
- **After:** New `POST /api/email-subscribe` endpoint implemented. Accepts `{email}`, validates (RFC-5322 regex, length ≤ 254 chars), respects existing unsubscribe status, and upserts into the existing `email_list` table via `db.track_email()`.
- Rate-limited: 10 requests per hour per IP.
- Logs a `email_subscribed` analytics event.

### F-05: SSE Streaming Respects `AI_PROVIDER` Setting
- **Before:** `/api/generate-dua-stream` always called `call_anthropic_stream()` regardless of the `AI_PROVIDER` environment variable. Users who set `AI_PROVIDER=openai` got Anthropic streaming (or errors if only an OpenAI key was configured).
- **After:** New `call_openai_stream()` async generator added with full OpenAI SSE protocol support (parses `data:` lines, handles `[DONE]` sentinel, detects `finish_reason: "length"` truncation with auto-ameen fallback). The streaming endpoint now dispatches to the correct provider based on `AI_PROVIDER`.
- Startup log now prints which streaming provider is active.
- Startup warning emitted if the configured provider's API key is missing.

### Files Changed
- **`app.py`** — all three fixes, version bump to 1.5.6
- **`README.md`** — version bump, version history updated
- **`CHANGELOG.md`** — this entry

### Backward Compatibility
- **No frontend changes** — `index.html` is untouched; the existing `submitPostGenEmail()` call now succeeds instead of silently failing.
- **No new environment variables** — all fixes use existing configuration (`SECRET_KEY`, `ADMIN_PASSWORD`, `AI_PROVIDER`).
- **Admin sessions** — existing users will need to re-login (one-time) since the auth mechanism changed from query param to cookie.
- **Web-only users unaffected** — all changes are backend-only.

---

## v1.5.5 — Capacitor Hybrid App Wrapper (March 2026)

Adds the infrastructure to wrap MyDua.AI as a native iOS + Android app via Capacitor, while maintaining full backward compatibility with the existing website.

### New Files
- **`native-bridge.js`** — drop-in Capacitor plugin wrapper (`MyDuaNative.*` API) with graceful web fallback for all functions: API routing, persistent storage, haptics, native share sheet, push notifications, GPS with sacred site geofencing, background audio, file saving, and deep links
- **`service-worker.js`** — offline caching with route-aware strategies (cache-first for app shell, network-first for API data, cache-on-play for audio); push notification display; offline fallback page
- **`manifest.json`** — PWA manifest with app icons, shortcuts ("How is your heart?" and "My Journey"), and theme colors
- **`package.json`** — 13 Capacitor plugins pre-configured for v2.0 roadmap (core, app, filesystem, haptics, network, preferences, push-notifications, share, splash-screen, status-bar, geolocation, browser, background-audio)
- **`capacitor.config.ts`** — Capacitor configuration matched to MyDua.AI dark theme, plugin settings, and allowed navigation domains
- **`native-ios/`** — Info.plist additions (background audio, location permissions with Hajj-specific descriptions, push), entitlements (associated domains, push, background audio)
- **`native-android/`** — AndroidManifest additions (location, audio foreground service, push, deep links)
- **`MIGRATION-GUIDE.md`** — 8-step walkthrough for initializing Capacitor, syncing platforms, and submitting to App Store / Play Store

### Frontend Changes (index.html)
- **`nFetch()` wrapper** — all `fetch()` calls to API endpoints now routed through native-aware helper that prepends base URL when running in Capacitor, passes through unchanged on web
- **PWA meta tags** — `manifest.json` link, `apple-mobile-web-app-capable`, `theme-color`, `apple-touch-icon`, `viewport-fit=cover`
- **Safe-area CSS** — `env(safe-area-inset-*)` padding on body for iPhone notch / Android cutout
- **Offline detection** delegated to Capacitor Network plugin (falls back to browser events on web)
- **Form auto-save** upgraded to Capacitor Preferences when available (persistent across app restarts), falls back to sessionStorage on web
- **Native bridge initialization** — `MyDuaNative.init()` called on DOMContentLoaded with platform-aware API base URL

### Backend Changes (app.py)
- **CORS origins expanded** — added `capacitor://localhost`, `http://localhost`, `https://localhost` for Capacitor WebView
- **CSP updated** — `connect-src` expanded for Capacitor origins, Google Analytics, and Google Tag Manager; `script-src` expanded for Google Tag Manager
- **Permissions-Policy** — `geolocation` changed from `()` (blocked) to `(self)` (allowed for Hajj/Umrah phase detection)
- **Version** bumped to 1.5.5 across docstring, FastAPI app, health endpoint, logger, and admin dashboard

### Backward Compatibility
- **Zero breaking changes on web** — every native bridge function checks `window.Capacitor.isNativePlatform()` and falls back to browser APIs when false
- `nFetch()` with empty base URL is identical to `fetch()` with relative paths
- Safe-area CSS evaluates to 0px on non-mobile browsers
- Service worker, manifest, and PWA meta tags are ignored by desktop browsers unless user explicitly installs

---

## v1.5.4 — Age Granularity, Expanded Concerns & COPPA Fix (March 2026)

- **Concern tags expanded from 14 to 28** — new tags across all 4 locales: Children, Parents, Family unity, Righteous offspring, Career, Rizq, Taqwa, Gratitude, Sincerity, Repentance, Afterlife, Peace of mind, Loneliness, Grief. Covers spiritual, family, career, and emotional du'a themes so users can tap instead of type.
- **Individual ages replace decade buckets** — `AGE_RANGES` expanded from 27 values (`Under 1`, `1`–`20`, `20s`–`70+`) to 82 values (`Under 1`, `1`–`80`, `80+`). Individual ages give the AI model much richer signal for personalizing du'a language.
- **`ageRange()` helper** — new utility function replaces hardcoded filter arrays; `ageRange(min, max?)` dynamically generates valid age options from `AGE_RANGES`, including proper handling of "Under 1" for child relationships.
- **Relationship age filters updated** — Mother/Father: 20–80+, Grandfather/Grandmother: 50–80+, Son/Daughter/Nephew/Niece: Under 1–80+, Wife/Husband: 18–80+, Teacher/Mentor/Colleague: 16–80+, Pet: Under 1–20.
- **COPPA fix** — "Under 1" age option removed from the user's own age dropdown; `parseInt("Under 1")` returns `NaN` which passed the `isNaN` check and leaked through. Now explicitly excluded. Family/friend cards still allow all ages.
- **Name & Relationship side-by-side** — member card layout changed to forced two-column grid so Name and Relationship fields stay horizontal at all screen sizes (no longer collapses on mobile).
- **Version** bumped to 1.5.4

---

## v1.5.3 — Full i18n, RTL Fixes, Admin Dashboard & Email Opt-in (March 2026)

- **Complete i18n overhaul** — all UI elements now translated: member card labels, concern tags, dropdowns, privacy notice, support section, post-gen email, result action buttons, email/SMS/share panels, AI disclaimer, closing note, footer, and seasonal banners
- **Arabic (ar) locale completed** — 50+ new keys covering every user-facing string
- **RTL fixes** — concern tag scroll arrows and fade gradients flip correctly in RTL; `Math.abs(scrollLeft)` for cross-browser RTL scroll detection; select dropdown chevron repositioned for RTL
- **Locale-aware `renderMembers()`** — member cards use translated labels for Name, Relationship, Age, Gender, concerns, and placeholders
- **Hijri-aware seasonal banners** — 20 Islamic occasions detected via `Intl.DateTimeFormat` Hijri calendar; banner text uses locale keys for full translation
- **Token budgets bumped ~50% across all tiers** — generous headroom (1.5× max word target + formatting overhead) to eliminate truncation
- **Admin dashboard** — new `/admin/stats` endpoint with password-protected visual analytics
- **Post-generation email capture** — email removed from top-of-funnel; opt-in prompt shown after du'a generation
- **Google Analytics** — G-EEB8KEP0VP tag added
- **`ADMIN_PASSWORD` env var** — set in `.env` to enable dashboard access
- **Version** bumped to 1.5.3

---

## v1.5.2 — Tier Calibration & Dynamic Concern Strategy (March 2026)

- **Du'a tiers recalibrated to reading-time targets** — Quick: 40-75 words (15-30s), Post Salah: 450-600 words (3-5min), Sujood: 1,200-1,500 words (8-10min), Laylatul Qadr: 4,500-5,000 words (~30min)
- **Superseding concern-density rule** — word count is the leading constraint; concern-handling strategy scales dynamically based on words-per-concern ratio (theme-only → grouped → brief → moderate → deep → expansion)
- **Expansion mode** — when few concerns meet a large word budget (e.g. 1 concern on Laylatul Qadr), the model explores from multiple spiritual angles to fill the reading time
- **Tier-scaled timeouts** — frontend AbortController and progress bar scale per tier (Quick: 1min/10s, Post Salah: 2min/20s, Sujood: 3min/45s, Laylatul Qadr: 6min/3min)
- **Backend httpx timeout** raised from 120s to 360s to support Laylatul Qadr generation
- **Token budgets recalibrated** — Quick solo: 200 tokens, Post Salah solo: 900, Sujood solo: 2,200, LQ solo: 7,000
- **Version** bumped to 1.5.2

---

## v1.5.1 — Progress Bar UX Fix (March 2026)

- **Progress bar timing fixed** — progress bar, scroll, and "Writing..." state now trigger on first actual stream chunk instead of HTTP response headers; eliminates 10-15s dead gap where users saw nothing after the bar completed
- **Multi-language support** — language selector (English, Español, اردو, العربية) with full RTL support
- **Fast-fail API key validation** — streaming endpoint checks API key before attempting generation; returns instant error instead of 2-minute timeout
- **Improved timeout error message** — guides users to check backend/API key configuration
- **Version** bumped to 1.5.1

---

## v1.5.0 — Audit Remediation Release (March 2026)

Addresses 28 prioritized items from 9 independent audits (Legal, Privacy, Security, Performance, UX, Reliability, Scalability, SEO, Content/Copy).

### Security
- **CSP + Permissions-Policy** headers added to all responses
- **CORS tightened** from `allow_methods=["*"]` to explicit `GET, POST, OPTIONS`
- **Error messages sanitized** — 12+ messages rewritten to hide internal details (API keys, SMTP creds, status codes, stack traces)
- **Health endpoint hardened** — no longer exposes provider type, email config, or SMTP_FROM_EMAIL
- **Pydantic 422 handler** — validation errors return user-friendly messages instead of raw field paths
- **Prompt injection filter expanded** — 20+ blocked phrases, 8 XML tag patterns
- **AI output validation** — detects system prompt leakage before returning to client
- **PII masking** — `mask_email()` and `mask_ip()` applied to all logger calls
- **Stripe webhook** — new `/api/stripe-webhook` endpoint with signature verification
- **TTS rate limiting** — 3 requests/hour per IP (ElevenLabs is expensive)
- **Retry-After header** on all 429 rate limit responses
- **Email validation strengthened** — RFC-5322 regex on both frontend and backend

### Legal & Compliance
- **Terms of Service** — new `/terms` route with AI disclaimer, COPPA age requirement, donation disclaimer
- **CAN-SPAM unsubscribe** — `/unsubscribe` route with HMAC token verification
- **List-Unsubscribe headers** on all outbound emails (Resend + SMTP)
- **CAN-SPAM email footer** auto-appended to every email
- **Opt-out model** — users are opted-in by default; checkbox replaced with unsubscribe notice
- **Unsubscribe status check** — auto-email skips unsubscribed users
- **COPPA age gating** — primary user age dropdown filters out under-13
- **AI-generated content disclaimer** shown above du'a output
- **Donation disclaimer** — "not tax-deductible, non-refundable" in support section
- **Privacy policy updated** — removed "Malani Enterprise" and personal name references
- **Footer links** — Privacy Policy + Terms of Service added to frontend footer

### Performance & SEO
- **GZip compression** via Starlette middleware (500 byte threshold)
- **robots.txt + sitemap.xml** routes for search engines
- **OG + Twitter Card** meta tags (og:site_name, twitter:card, twitter:title, twitter:description, twitter:image)
- **html2pdf.js** loaded with `defer` attribute
- **Anthropic prompt caching** fixed — `cache_control` moved to system message block format; added to streaming endpoint
- **Cache key** now includes occasion + tier (previously only hashed members)

### UX
- **Auto-PDF removed** — PDF only via explicit button click
- **SSE timeout** — AbortController cancels after 120s with user-friendly message
- **Rate limit recovery** — shows Retry-After countdown from server header
- **Offline detection** — disables Generate button when network drops, re-enables on reconnect
- **Form auto-save** — saves to sessionStorage every 5s, restores on page load
- **Progress indicator** — multi-stage messages during generation
- **Brand standardized** — "MyDua.AI" across 30+ locations (was inconsistent mydua.ai / MyDua.ai)

### Backend Architecture
- **`db_async()` helper** for wrapping synchronous SQLite calls in `asyncio.to_thread`
- **DB schema migration** — `unsubscribed` and `unsubscribed_at` columns added to email_list
- **New analytics events** — `donations_completed`, `donations_expired`, `unsubscribes`
- **Version** bumped to 1.5.0 in docstring, FastAPI app, health endpoint, logger

### Manual Follow-ups
- `.DS_Store` — delete locally (`rm .DS_Store`), already in .gitignore
- **CAN-SPAM physical address** — add to email footer once available
- **`STRIPE_WEBHOOK_SECRET`** — set in `.env` for webhook signature verification

---

## v1.4.4

- Du'a length toggle (Quick / Standard / Detailed)
- Solo user mode with flowing personal du'a
- Concern tags on family member cards
- Edit & Regenerate button
- Retry logic on AI API calls
- Security headers (HSTS, X-Frame-Options, etc.)
- Rate limiting on all tracking endpoints
- Analytics endpoint protected with ANALYTICS_KEY
