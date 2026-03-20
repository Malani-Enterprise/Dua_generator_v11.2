# MyDua.AI

**Version 1.5.5** | March 2026

A du'a lives deep in your heart. We help it find its words — personalized Islamic supplications for every occasion, powered by Quran and authentic Hadith.

## Overview

MyDua.AI is an AI-powered web and mobile application that generates deeply personalized du'as (Islamic supplications) for individuals and families. The platform uses Claude (Anthropic) or GPT-4o (OpenAI) combined with Quranic verses and authentic Hadith to create intimate, spiritually resonant prayers tailored to each person's age, relationships, life concerns, and chosen occasion.

## Features

**Du'a Generation**: AI-powered personalized supplications with real-time SSE streaming, supporting 11 occasions (Ramadan, Hajj/Umrah, Jumu'ah, illness, exams, travel, new baby, marriage, grief, gratitude, and general), 4 length tiers (Quick, Post Salah, Sujood, Laylatul Qadr), and 28 concern tags across spiritual, family, career, and emotional themes.

**Family Personalization**: Add up to 15 family members with individual names, relationships, ages, genders, and specific prayer concerns. The AI weaves each person's details naturally throughout the du'a.

**Multi-Language**: Full support for English, Spanish (Espanol), Urdu, and Arabic with RTL layout and locale-aware UI.

**Sharing & Delivery**: Shareable links, email delivery (Resend/SMTP), SMS (Twilio), WhatsApp/Facebook/Instagram/TikTok sharing, PDF export (html2pdf.js), and physical postcard delivery (Lob).

**Gift System**: Send personalized du'as as gifts via email, SMS, or postcard with dedicated gift viewing pages.

**Audio (TTS)**: AI-generated voice recitation via ElevenLabs with browser Web Speech API fallback.

**Mobile App Ready (v1.5.5)**: Capacitor hybrid app infrastructure for iOS and Android with native bridge, service worker for offline caching, PWA manifest, GPS sacred site detection, background audio, push notifications, and haptic feedback — all with graceful web fallback.

## Tech Stack

**Backend**: FastAPI 0.115.0 (Python), SQLite with WAL mode, Uvicorn

**AI**: Anthropic Claude (default) or OpenAI GPT-4o

**Frontend**: Single-file HTML/JS/CSS (vanilla), Cormorant Garamond + Amiri fonts

**Mobile**: Capacitor v6 hybrid wrapper (13 plugins)

**Integrations**: Stripe (payments), Resend/SMTP (email), Twilio (SMS), Lob (postcards), ElevenLabs (TTS), Google Analytics

## Quick Start

```bash
# 1. Clone and enter the project
cd MyDua-AI-v1.5.5-production

# 2. Create environment file
cp .env.example .env
# Edit .env with your API keys (at minimum: ANTHROPIC_API_KEY or OPENAI_API_KEY)

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Run the server
uvicorn app:app --host 0.0.0.0 --port 8000

# 5. Open in browser
open http://localhost:8000
```

## Mobile App Setup (Capacitor)

The Capacitor infrastructure is included but the native projects are not yet generated. See `MIGRATION-GUIDE.md` for the complete 8-step setup process. Summary:

```bash
# 1. Install Node dependencies
npm install

# 2. Generate native projects
npx cap add ios
npx cap add android

# 3. Sync web assets
npx cap sync

# 4. Open in IDE
npx cap open ios      # Opens Xcode
npx cap open android  # Opens Android Studio
```

**Prerequisites for mobile**: macOS (for iOS), Xcode 15+, Android Studio, Node.js 18+, Apple Developer Account ($99/year), Google Play Account ($25 one-time).

## Project Structure

```
MyDua-AI-v1.5.5-production/
├── app.py                  # Backend API (FastAPI)
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variable template
├── .gitignore              # Git ignore rules
├── CHANGELOG.md            # Version history
├── MIGRATION-GUIDE.md      # Capacitor setup walkthrough
├── RELEASE-NOTES-v1.5.5.docx  # Formal release notes
├── README.md               # This file
├── package.json            # Node/Capacitor dependencies
├── capacitor.config.ts     # Capacitor configuration
├── locales/
│   └── en.json             # English locale strings
├── static/
│   ├── index.html          # Frontend (single-file SPA)
│   ├── native-bridge.js    # Capacitor plugin wrappers
│   ├── service-worker.js   # Offline caching + push
│   ├── manifest.json       # PWA manifest
│   ├── favicon.svg         # Browser favicon
│   ├── og-image.png        # Social sharing image
│   ├── og-image.svg        # Social sharing image (vector)
│   └── icons/              # App icons (generate from logo)
├── native-ios/
│   ├── Info.plist.additions.xml     # iOS permissions
│   └── Entitlements.additions.plist # iOS entitlements
└── native-android/
    └── AndroidManifest.additions.xml # Android permissions
```

## Environment Variables

See `.env.example` for the complete list. Required:

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes* | Anthropic Claude API key |
| `OPENAI_API_KEY` | Yes* | OpenAI API key (*one of the two is required) |
| `AI_PROVIDER` | Yes | "anthropic" or "openai" |
| `APP_BASE_URL` | Yes | Your deployed URL (e.g., https://mydua.ai) |
| `SECRET_KEY` | Yes | Random string, 32+ characters |

Optional integrations: SMTP (email), Stripe (payments), Twilio (SMS), Lob (postcards), ElevenLabs (TTS), Firebase (push notifications).

## API Endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/api/generate-dua-stream` | Generate du'a via SSE streaming |
| POST | `/api/save-dua` | Save du'a and get shareable URL |
| POST | `/api/email-dua` | Email a saved du'a |
| POST | `/api/create-support-session` | Create Stripe payment session |
| GET | `/shared/{dua_id}` | Public share page |
| GET | `/admin/stats` | Admin analytics dashboard |
| GET | `/privacy` | Privacy policy |
| GET | `/terms` | Terms of service |

## Version History

See `CHANGELOG.md` for the complete version history. Recent releases:

- **v1.5.5** — Capacitor hybrid app wrapper (this release)
- **v1.5.4** — Age granularity, expanded concerns, COPPA fix
- **v1.5.3** — Full i18n, RTL fixes, admin dashboard, email opt-in
- **v1.5.2** — Tier calibration, dynamic concern strategy
- **v1.5.1** — Progress bar UX fix, multi-language support
- **v1.5.0** — Audit remediation (security, legal, performance, UX)
- **v1.4.4** — Du'a length toggle, solo mode, concern tags

## License

Proprietary. All rights reserved.
