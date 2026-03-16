# Changelog

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
