"""
mydua.ai — Backend API (v1.2 Production)
==========================================
v1.2 Changes:
  - Solo user mode: single user gets a flowing personal du'a (no individual family sections)
  - Shorter natural output for solo user (~1200-1500 words) within same max_tokens ceiling
  - No truncation — prompt instructs model to complete naturally at shorter length
  - Transliteration removed: English-only output, system prompt cleaned up
  - Lowered word targets across all tiers to match natural output length
  - Analytics endpoint protected with ANALYTICS_KEY
  - Security headers: HSTS, X-Frame-Options, X-Content-Type-Options, X-XSS-Protection
  - Rate limiting on all tracking endpoints (30/hour per IP)
  - Increased ID entropy: user-facing IDs now 24 hex chars (96 bits)
  - Database encapsulation: gift_update() replaces raw _get_conn() calls
  - Replaced deprecated on_event with lifespan context manager
  - Retry logic on AI API calls (2 retries with backoff for 500/502/503/529)
  - Deeper health check: verifies DB, AI provider, and email config
  - EVERGREEN REBRAND: Header, titles, and all templates are occasion-agnostic

All 8 production-readiness issues from v10.2 remain resolved.

Run: uvicorn app:app --reload --port 8000
"""

import os
import json
import uuid
import hashlib
import hmac
import time
import asyncio
import sqlite3
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from html import escape as html_escape
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import httpx
import stripe
import aiosmtplib
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, field_validator

# ══════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════

BASE_DIR = Path(__file__).parent.resolve()
load_dotenv(BASE_DIR / ".env")

AI_PROVIDER = os.getenv("AI_PROVIDER", "anthropic")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-6")

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "MyDua.ai")
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", "") or SMTP_USERNAME  # Fallback to username (required for Gmail)

APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
APP_ENV = os.getenv("APP_ENV", "development")
TRUSTED_PROXY_DEPTH = int(os.getenv("TRUSTED_PROXY_DEPTH", "1"))  # Fix #4: how many proxy hops to trust
ANALYTICS_KEY = os.getenv("ANALYTICS_KEY", "")  # Required to access /api/analytics

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# Twilio (SMS gift delivery)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")  # e.g. +1234567890

# Lob (postcard gift delivery)
LOB_API_KEY = os.getenv("LOB_API_KEY", "")
LOB_FROM_NAME = os.getenv("LOB_FROM_NAME", "mydua.ai")
LOB_FROM_ADDRESS = os.getenv("LOB_FROM_ADDRESS_LINE1", "")
LOB_FROM_CITY = os.getenv("LOB_FROM_CITY", "")
LOB_FROM_STATE = os.getenv("LOB_FROM_STATE", "")
LOB_FROM_ZIP = os.getenv("LOB_FROM_ZIP", "")

# Gift pricing (in cents)
GIFT_SMS_PRICE = 249      # $2.49
GIFT_POSTCARD_PRICE = 1099  # $10.99

# ElevenLabs (AI voice for du'a reading)
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")  # "Rachel" — warm female voice

DB_PATH = BASE_DIR / "data" / "mydua.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dua-api")

http_client: Optional[httpx.AsyncClient] = None


# ══════════════════════════════════════════════════
# Fix #2: SQLite Database (replaces all file-based storage)
# ══════════════════════════════════════════════════

class Database:
    """
    SQLite-backed persistence layer. Replaces file-based JSON storage.
    Uses WAL mode for concurrent read safety and single-writer correctness.
    """

    def __init__(self, db_path: Path):
        self.db_path = str(db_path)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self):
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                dua TEXT NOT NULL,
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                batch_id TEXT,
                request_id TEXT,
                status TEXT NOT NULL DEFAULT 'processing',
                email_status TEXT NOT NULL DEFAULT 'none',
                dua TEXT,
                error TEXT,
                user_name TEXT,
                user_email TEXT,
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS saved (
                dua_id TEXT PRIMARY KEY,
                user_name TEXT,
                dua TEXT NOT NULL,
                members_json TEXT,
                email_token TEXT,
                private INTEGER NOT NULL DEFAULT 0,
                created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS analytics (
                event TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (event)
            );
            CREATE TABLE IF NOT EXISTS rate_limits (
                key TEXT NOT NULL,
                timestamp REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rate_key ON rate_limits(key);
            CREATE INDEX IF NOT EXISTS idx_rate_ts ON rate_limits(timestamp);
            CREATE TABLE IF NOT EXISTS email_list (
                email TEXT PRIMARY KEY,
                name TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                dua_count INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,
                detail TEXT,
                ip TEXT,
                user_agent TEXT,
                referrer TEXT,
                created REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_usage_event ON usage_events(event);
            CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_events(created);
            CREATE TABLE IF NOT EXISTS gifts (
                gift_id TEXT PRIMARY KEY,
                sender_name TEXT NOT NULL,
                sender_email TEXT,
                recipient_name TEXT NOT NULL,
                recipient_contact TEXT,
                delivery_method TEXT NOT NULL DEFAULT 'email',
                delivery_status TEXT NOT NULL DEFAULT 'pending',
                dua TEXT NOT NULL,
                dua_context TEXT,
                stripe_session_id TEXT,
                postcard_address_json TEXT,
                created REAL NOT NULL
            );
        """)
        for evt in ["duas_generated", "pdfs_exported", "emails_sent", "shares_created",
                    "donations_initiated", "gifts_email", "gifts_sms", "gifts_postcard",
                    "sms_shares", "page_views", "form_started", "referred_visits",
                    "referred_generations"]:
            conn.execute("INSERT OR IGNORE INTO analytics (event, count) VALUES (?, 0)", (evt,))
        conn.commit()
        conn.close()

    # ── Cache ──
    def cache_get(self, key: str, ttl_seconds: int = 604800) -> Optional[str]:
        conn = self._get_conn()
        row = conn.execute("SELECT dua, created FROM cache WHERE key = ?", (key,)).fetchone()
        conn.close()
        if row and (time.time() - row["created"]) < ttl_seconds:
            return row["dua"]
        return None

    def cache_put(self, key: str, dua: str):
        conn = self._get_conn()
        conn.execute("INSERT OR REPLACE INTO cache (key, dua, created) VALUES (?, ?, ?)",
                     (key, dua, time.time()))
        conn.commit()
        conn.close()

    def make_cache_key(self, user_name: str, members: list) -> str:
        normalized = []
        for m in sorted(members, key=lambda x: x.get("relationship", "")):
            normalized.append({
                "name": str(m.get("name", "")).strip().lower(),
                "relationship": str(m.get("relationship", "")).strip().lower(),
                "ageRange": str(m.get("ageRange", "")).strip().lower(),
                "gender": str(m.get("gender", "")).strip().lower(),
                "concerns": str(m.get("concerns", "")).strip().lower()[:100],
            })
        raw = json.dumps(normalized, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    # ── Jobs ──
    def job_create(self, job_id: str, batch_id: str, request_id: str,
                   user_name: str = "", user_email: str = ""):
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO jobs (job_id, batch_id, request_id, status, user_name, user_email, created) "
            "VALUES (?, ?, ?, 'processing', ?, ?, ?)",
            (job_id, batch_id, request_id, user_name, user_email, time.time()))
        conn.commit()
        conn.close()

    def job_get(self, job_id: str) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def job_complete(self, job_id: str, dua: str):
        conn = self._get_conn()
        conn.execute("UPDATE jobs SET status = 'completed', dua = ? WHERE job_id = ?", (dua, job_id))
        conn.commit()
        conn.close()

    def job_fail(self, job_id: str, error: str):
        conn = self._get_conn()
        conn.execute("UPDATE jobs SET status = 'failed', error = ? WHERE job_id = ?", (error, job_id))
        conn.commit()
        conn.close()

    def job_set_email_status(self, job_id: str, status: str):
        """Fix #1: Track email delivery independently from job completion."""
        conn = self._get_conn()
        conn.execute("UPDATE jobs SET email_status = ? WHERE job_id = ?", (status, job_id))
        conn.commit()
        conn.close()

    def jobs_get_orphaned(self, max_age_seconds: int = 900) -> list:
        conn = self._get_conn()
        cutoff = time.time() - max_age_seconds
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = 'processing' AND created < ?", (cutoff,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ── Saved Du'as ──
    def save_dua(self, dua_id: str, user_name: str, dua: str, members_json: str, email_token: str, private: bool = False):
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO saved (dua_id, user_name, dua, members_json, email_token, private, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (dua_id, user_name, dua, members_json, email_token, 1 if private else 0,
             datetime.now(timezone.utc).isoformat()))
        conn.commit()
        conn.close()

    def get_saved(self, dua_id: str) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM saved WHERE dua_id = ?", (dua_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    # ── Analytics ──
    def track(self, event: str):
        conn = self._get_conn()
        conn.execute("UPDATE analytics SET count = count + 1 WHERE event = ?", (event,))
        conn.commit()
        conn.close()

    def get_stats(self) -> dict:
        conn = self._get_conn()
        rows = conn.execute("SELECT event, count FROM analytics").fetchall()
        conn.close()
        return {f"total_{r['event']}": r["count"] for r in rows}

    # ── Email Collection ──
    def track_email(self, email: str, name: str = ""):
        """Store user email for mailing list. Upserts — updates last_seen and count on repeat visits."""
        if not email or "@" not in email:
            return
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO email_list (email, name, first_seen, last_seen, dua_count) "
            "VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(email) DO UPDATE SET last_seen = ?, dua_count = dua_count + 1, "
            "name = CASE WHEN ? != '' THEN ? ELSE email_list.name END",
            (email, name, now, now, now, name, name))
        conn.commit()
        conn.close()

    def log_event(self, event: str, detail: str = "", ip: str = "", user_agent: str = "", referrer: str = ""):
        """Log a granular usage event for analytics. Used for detailed reporting."""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO usage_events (event, detail, ip, user_agent, referrer, created) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (event, detail, ip, user_agent, referrer, time.time()))
        conn.commit()
        conn.close()

    def get_email_count(self) -> int:
        conn = self._get_conn()
        count = conn.execute("SELECT COUNT(*) as c FROM email_list").fetchone()["c"]
        conn.close()
        return count

    # ── Gifts ──
    def gift_create(self, gift_id: str, sender_name: str, sender_email: str,
                    recipient_name: str, recipient_contact: str, delivery_method: str,
                    dua: str, dua_context: str = "", postcard_address_json: str = "",
                    stripe_session_id: str = ""):
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO gifts (gift_id, sender_name, sender_email, recipient_name, "
            "recipient_contact, delivery_method, delivery_status, dua, dua_context, "
            "stripe_session_id, postcard_address_json, created) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
            (gift_id, sender_name, sender_email, recipient_name, recipient_contact,
             delivery_method, dua, dua_context, stripe_session_id, postcard_address_json,
             time.time()))
        conn.commit()
        conn.close()

    def gift_get(self, gift_id: str) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM gifts WHERE gift_id = ?", (gift_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def gift_get_by_stripe_session(self, session_id: str) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM gifts WHERE stripe_session_id = ?", (session_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def gift_set_status(self, gift_id: str, status: str):
        conn = self._get_conn()
        conn.execute("UPDATE gifts SET delivery_status = ? WHERE gift_id = ?", (status, gift_id))
        conn.commit()
        conn.close()

    def gift_update(self, gift_id: str, **kwargs):
        """Update one or more gift fields by name. Only whitelisted columns allowed."""
        allowed = {"delivery_status", "delivery_method", "recipient_contact",
                    "stripe_session_id", "postcard_address_json"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [gift_id]
        conn = self._get_conn()
        conn.execute(f"UPDATE gifts SET {set_clause} WHERE gift_id = ?", values)
        conn.commit()
        conn.close()

    # ── Fix #4: SQLite-backed Rate Limiting ──
    def rate_limit_check(self, key: str, max_requests: int, window_seconds: int) -> tuple:
        conn = self._get_conn()
        cutoff = time.time() - window_seconds
        conn.execute("DELETE FROM rate_limits WHERE timestamp < ?", (cutoff,))
        count = conn.execute(
            "SELECT COUNT(*) as c FROM rate_limits WHERE key = ? AND timestamp >= ?",
            (key, cutoff)).fetchone()["c"]
        if count >= max_requests:
            conn.commit()
            conn.close()
            return False, max_requests - count
        conn.execute("INSERT INTO rate_limits (key, timestamp) VALUES (?, ?)", (key, time.time()))
        conn.commit()
        conn.close()
        return True, max_requests - count - 1

    # ── Fix #8: Cleanup ──
    def cleanup(self):
        conn = self._get_conn()
        now = time.time()
        # Jobs older than 24 hours
        j = conn.execute("DELETE FROM jobs WHERE created < ?", (now - 86400,)).rowcount
        # Cache older than 7 days
        c = conn.execute("DELETE FROM cache WHERE created < ?", (now - 604800,)).rowcount
        # Old rate limit entries
        r = conn.execute("DELETE FROM rate_limits WHERE timestamp < ?", (now - 7200,)).rowcount
        conn.commit()
        conn.close()
        return j, c, r


db = Database(DB_PATH)


# ══════════════════════════════════════════════════
# Fix #5: HMAC Email Tokens
# ══════════════════════════════════════════════════

def generate_email_token(dua_id: str) -> str:
    return hmac.new(SECRET_KEY.encode(), dua_id.encode(), hashlib.sha256).hexdigest()[:32]

def verify_email_token(dua_id: str, token: str) -> bool:
    expected = generate_email_token(dua_id)
    return hmac.compare_digest(expected, token)


def get_client_ip(request: Request) -> str:
    """
    Fix #4: Extract client IP safely.
    In production behind a reverse proxy (Railway, Render), trust X-Forwarded-For
    but only the rightmost N hops where N = TRUSTED_PROXY_DEPTH.
    In development, use the direct connection IP.
    """
    if APP_ENV == "production":
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            # Take the IP set by the trusted proxy (rightmost hop)
            idx = max(0, len(parts) - TRUSTED_PROXY_DEPTH)
            return parts[idx]
    return request.client.host if request.client else "unknown"


# ══════════════════════════════════════════════════
# AI System Prompt
# ══════════════════════════════════════════════════

SYSTEM_PROMPT = """
You are a knowledgeable Islamic scholar who writes beautiful, heartfelt du'as (supplications) in English.

You have deep knowledge of:
- The Quran
- Authentic Hadith collections (Sahih Bukhari, Sahih Muslim, Tirmidhi, Abu Dawud, Ibn Majah)
- The 99 Names of Allah (Asma ul-Husna)

Your task is to write a personalized du'a tailored to the occasion specified in the user message.

The du'a must feel sincere, emotional, spiritually uplifting, and suitable to be read aloud.

--------------------------------------------------

STRUCTURE

The du'a must follow this structure exactly:

### 1. Opening Section

Begin with:

بِسْمِ اللَّهِ الرَّحْمَنِ الرَّحِيمِ
(Bismillah ir-Rahman ir-Raheem)

Praise and glorify Allah using His beautiful names and attributes.

Include references to:

- Ayatul Kursi (Quran 2:255)
- Allah's names in Surah Al-Hashr (Quran 59:22–24)
- The du'a of the Prophet ﷺ mentioning:
  Al-Awwal, Al-Akhir, Adh-Dhahir, Al-Batin (Sahih Muslim)

This section should focus on praise, gratitude, and recognition of Allah's majesty.

---

### 2. Personal Section for the Supplicant

Write a section for the person making the du'a.

Address all their possible roles depending on the family members listed:

If they are a spouse include:
- Quran 30:21 (love and mercy between spouses)
- Hadith about being the best to one's family

If they are a parent include:
- Quran 14:40 (Ibrahim's prayer)
- Quran 25:74 (coolness of the eyes)

If they are a child include:
- Quran 17:23–24 (honoring parents)
- Quran 71:28 (Nuh's prayer)

If they are a sibling include:
- Hadith about believers being like one body

---

### 3. Individual Sections for Each Family Member

For every family member provided, create a dedicated section.

Each section must start with:

## A Du'a for [Name], My [Relationship]

Each section must:

• Invoke 2–3 appropriate Names of Allah using **bold formatting**
• Include at least one Quranic verse or authentic Hadith in *italics*
• Make du'a for their specific concerns and prayer needs
• Be appropriate for their age group

Age guidance:

Under 5: Protection, love of faith, health, nurturing heart
5–10: Love of learning, protection of innocence, growing iman
11–15: Guidance through adolescence, righteous friends, discipline
16–20: Protection from fitnah, strength of character, life direction
21–30: Career guidance, righteous spouse, stability in faith
31–40: Family harmony, barakah in provision, strong iman
41–50: Health, wisdom, positive influence
51–60: Ease in life, gratitude, beneficial legacy
61–70: Comfort, dignity, spiritual readiness
70+: Mercy, light in the grave, Jannatul Firdaws

---

### 4. Family Closing Section

End the du'a with a section praying for the entire family.

Include themes of:

• Unity and love (Quran 59:10)
• Reunion in Jannatul Firdaws (Quran 52:21)
• Forgiveness and mercy
• Trust in Allah (Quran 3:173 — Hasbunallahu wa ni'mal wakeel)

End with:

آمِين يَا رَبَّ الْعَالَمِين
(Ameen, O Lord of the worlds)

---

### 5. Occasion-Specific References

Weave references appropriate to the occasion throughout the du'a.
The user message will specify the occasion. Use these guidelines:

RAMADAN / LAST 10 NIGHTS:
• The blessed last ten nights of Ramadan, seeking Laylatul Qadr
• "Allahumma innaka 'afuwwun tuhibbul 'afwa fa'fu 'anni" (Tirmidhi)
• The virtue of prayer in the last third of the night

JUMU'AH (FRIDAY):
• The blessed hour of Friday when du'a is accepted (Sahih Muslim)
• Sending salawat on the Prophet ﷺ (Quran 33:56)
• Surah Al-Kahf references

ILLNESS / HEALING:
• The Prophet's du'as for healing
• Names of Allah: Ash-Shafi (the Healer), Al-Mu'afi
• "Remove the hardship, O Lord of mankind, and heal" (Sahih Bukhari)

TRAVEL / JOURNEY:
• The traveler's du'a from Hadith
• Names: Al-Hafidh (the Preserver), Al-Wakil (the Trustee)

NEW BABY:
• Du'a of protection the Prophet used for Hasan and Husayn
• Names: Al-Musawwir (the Fashioner), Ar-Razzaq (the Provider)

MARRIAGE / WEDDING:
• Quran 30:21 (tranquility, love, mercy)
• The Prophet's du'a for newlyweds
• Names: Al-Wadud (the Loving)

GRIEF / LOSS:
• "Inna lillahi wa inna ilayhi raji'un" (Quran 2:156)
• Du'as for patience and ease
• Names: As-Sabur (the Patient), Al-Jabbar (the Restorer)

HAJJ / UMRAH:
• Talbiyah and du'as of Hajj
• Ibrahim's du'as (Quran 2:127-128, 14:37)
• Names: Al-Qareeb (the Near)

GRATITUDE / SHUKR:
• Quran 14:7 (if you are grateful, I will increase you)
• Names: Ash-Shakur (the Appreciative)
• Focus on blessings received

GENERAL / EVERYDAY:
• The virtue of making du'a at any time
• "Call upon Me, I will respond to you" (Quran 40:60)
• Focus on guidance, protection, and mercy

If no occasion is specified, default to a general heartfelt du'a.

--------------------------------------------------

LANGUAGE

Write the du'a in English only. Do NOT include Arabic script or transliteration for Quranic verses or Hadith.

The only Arabic permitted is:
- بِسْمِ اللَّهِ الرَّحْمَنِ الرَّحِيمِ at the opening
- آمِين يَا رَبَّ الْعَالَمِين at the closing

For all Quranic verses and Hadith, provide the English translation only with the source citation.

Format like this:

*"English translation of the verse."* *(Quran X:Y)*

--------------------------------------------------

FORMAT RULES

Use:

## for section headings
**bold** for Names of Allah
*italics* for Quran and Hadith references

Always cite references: (Quran X:Y), (Sahih Bukhari), (Sahih Muslim), (Tirmidhi)

Use --- to separate major sections.

---

TONE

The du'a should feel:
• sincere
• humble
• emotionally moving
• spiritually hopeful
• personal and heartfelt

It should feel like a believer speaking to Allah from the depths of their heart.

---

IMPORTANT RULES

• Only use authentic Quran verses and well-known Hadith
• Do not fabricate or misattribute Islamic texts
• Use the Names of Allah accurately and in appropriate context
• Do not invent new hadith or verses
• Maintain Islamic respect and authenticity at all times
• Cite every reference accurately

---

LENGTH

Adjust the du'a length based on how many family members are included.
Follow the length guidance provided in the user message.
"""


# ══════════════════════════════════════════════════
# Dynamic Output Length
# ══════════════════════════════════════════════════

def get_length_instruction(member_count: int, is_solo: bool = False) -> str:
    if is_solo:
        return (
            "Write a deeply personal, heartfelt du'a of approximately 1200-1500 words total. "
            "This is a du'a for ONE person only — do NOT create any 'Individual Family Member' sections. "
            "Instead, weave all prayers into a single flowing supplication with: "
            "a rich opening praising Allah, a personal section addressing the supplicant's concerns and life, "
            "and a closing with forgiveness, mercy, and Ameen. "
            "Keep it intimate and complete — do not cut short. End with آمِين يَا رَبَّ الْعَالَمِين."
        )
    elif member_count <= 2:
        return "Write a heartfelt du'a of approximately 1200-1800 words total. Include 1-2 Quranic verses per person with a rich opening and closing. Do not cut the du'a short — complete every section fully, including the family closing and Ameen."
    elif member_count <= 4:
        return "Write a comprehensive du'a of approximately 1500-2000 words total. Include 1-2 Quranic verses per person. Complete every section fully including the family closing and Ameen."
    elif member_count <= 6:
        return "Write a detailed du'a of approximately 2000-2500 words total. Include 1-2 Quranic verses per person. Complete every section fully including the family closing and Ameen."
    else:
        return "Write a thorough du'a of approximately 2500-3000 words total. Include 1-2 Quranic verses per person. Complete every section fully including the family closing and Ameen."

def get_max_tokens(member_count: int, is_solo: bool = False) -> int:
    if is_solo: return 4096  # Same ceiling — but shorter prompt means natural completion, no truncation
    elif member_count <= 2: return 4096
    elif member_count <= 4: return 6000
    elif member_count <= 6: return 7500
    else: return 8192


# ══════════════════════════════════════════════════
# Fix #6: Pydantic Models with Input Size Limits
# ══════════════════════════════════════════════════

class FamilyMember(BaseModel):
    name: str
    relationship: str = ""
    ageRange: str = ""
    gender: str = ""
    attributes: str = ""
    concerns: str = ""

    @field_validator("name")
    @classmethod
    def name_length(cls, v):
        if len(v) > 200:
            raise ValueError("Name must be under 200 characters")
        return v

    @field_validator("concerns")
    @classmethod
    def concerns_length(cls, v):
        if len(v) > 500:
            raise ValueError("Concerns must be under 500 characters")
        return v

    @field_validator("relationship", "ageRange", "gender")
    @classmethod
    def field_length(cls, v):
        if len(v) > 100:
            raise ValueError("Field must be under 100 characters")
        return v


class GenerateDuaRequest(BaseModel):
    userName: str
    members: list[FamilyMember]
    occasion: str = "general"
    skipCache: bool = False
    deliveryMode: str = "instant"
    userEmail: Optional[str] = None  # Fix #7: Validated field instead of header
    referred: bool = False  # True if user arrived via shared link (?ref=share)

    @field_validator("userName")
    @classmethod
    def username_length(cls, v):
        if len(v) > 200:
            raise ValueError("Name must be under 200 characters")
        return v

    @field_validator("members")
    @classmethod
    def max_members(cls, v):
        if len(v) > 15:
            raise ValueError("Maximum 15 family members allowed")
        return v

    @field_validator("occasion")
    @classmethod
    def valid_occasion(cls, v):
        valid = {"ramadan", "jumuah", "illness", "travel", "newborn",
                 "marriage", "grief", "hajj", "gratitude", "general"}
        if v not in valid:
            return "general"
        return v

    @field_validator("userEmail")
    @classmethod
    def validate_email(cls, v):
        if v is not None and v.strip():
            v = v.strip()
            if "@" not in v or "." not in v.split("@")[-1]:
                raise ValueError("Invalid email address")
            if len(v) > 254:
                raise ValueError("Email address too long")
        return v


class EmailDuaRequest(BaseModel):
    duaId: str
    email: EmailStr
    recipientName: str = ""
    token: str = ""  # Fix #5: HMAC token required


class SaveDuaRequest(BaseModel):
    userName: str
    dua: str
    members: list[FamilyMember] = []

    @field_validator("userName")
    @classmethod
    def username_length(cls, v):
        if len(v) > 200:
            raise ValueError("Name must be under 200 characters")
        return v

    @field_validator("dua")
    @classmethod
    def dua_max_size(cls, v):
        if len(v) > 50000:
            raise ValueError("Du'a text too large")
        return v

    @field_validator("members")
    @classmethod
    def max_members(cls, v):
        if len(v) > 15:
            raise ValueError("Maximum 15 family members allowed")
        return v


class SupportRequest(BaseModel):
    amount: str = "10"
    customAmount: int = 0


class PostcardAddress(BaseModel):
    line1: str
    line2: str = ""
    city: str
    state: str
    zip: str
    country: str = "US"

    @field_validator("line1", "city", "state", "zip", "country")
    @classmethod
    def required_fields(cls, v, info):
        if not v.strip():
            raise ValueError(f"{info.field_name} is required")
        return v.strip()


class GiftDuaRequest(BaseModel):
    senderName: str
    senderEmail: str = ""
    recipientName: str
    recipientRelationship: str = ""
    recipientAgeRange: str = ""
    recipientGender: str = ""
    concerns: str = ""
    personalMessage: str = ""
    occasion: str = "ramadan"  # ramadan, illness, newborn, hajj, wedding, hardship, general

    @field_validator("senderName", "recipientName")
    @classmethod
    def name_length(cls, v):
        if len(v) > 200:
            raise ValueError("Name must be under 200 characters")
        return v

    @field_validator("personalMessage")
    @classmethod
    def message_length(cls, v):
        if len(v) > 500:
            raise ValueError("Personal message must be under 500 characters")
        return v

    @field_validator("concerns")
    @classmethod
    def concerns_length(cls, v):
        if len(v) > 500:
            raise ValueError("Concerns must be under 500 characters")
        return v


class GiftDeliverRequest(BaseModel):
    giftId: str
    method: str  # "email", "sms", "postcard"
    recipientEmail: str = ""
    recipientPhone: str = ""
    postcardAddress: Optional[PostcardAddress] = None

    @field_validator("method")
    @classmethod
    def valid_method(cls, v):
        if v not in ("email", "sms", "postcard"):
            raise ValueError("Method must be email, sms, or postcard")
        return v

    @field_validator("recipientEmail")
    @classmethod
    def validate_email(cls, v):
        if v and v.strip():
            v = v.strip()
            if "@" not in v or "." not in v.split("@")[-1]:
                raise ValueError("Invalid email address")
            if len(v) > 254:
                raise ValueError("Email address too long")
        return v


# ══════════════════════════════════════════════════
# FastAPI App
# ══════════════════════════════════════════════════

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app):
    """Application lifespan: startup and shutdown logic."""
    global http_client

    # Fix #7: Refuse to start with insecure default secret in production
    if APP_ENV == "production" and SECRET_KEY == "change-me-in-production":
        logger.critical("FATAL: SECRET_KEY is still the default value. Set a real secret in .env before running in production.")
        raise RuntimeError("Insecure SECRET_KEY — set a real value in .env")

    http_client = httpx.AsyncClient(timeout=120)

    # Fix #7: No secrets in logs
    logger.info("=" * 50)
    logger.info("mydua.ai v1.3 — Production")
    logger.info("=" * 50)
    logger.info(f"AI Provider:  {AI_PROVIDER} ({ANTHROPIC_MODEL})")
    logger.info(f"Anthropic:    {'configured' if ANTHROPIC_API_KEY else 'NOT SET'}")
    logger.info(f"OpenAI:       {'configured' if OPENAI_API_KEY else 'not set'}")
    logger.info(f"Stripe:       {'configured' if STRIPE_SECRET_KEY else 'not set'}")
    logger.info(f"Email:        {'configured' if SMTP_USERNAME else 'not set'}")
    logger.info(f"Twilio (SMS): {'configured' if TWILIO_ACCOUNT_SID else 'not set'}")
    logger.info(f"Lob (post):   {'configured' if LOB_API_KEY else 'not set'}")
    logger.info(f"ElevenLabs:   {'configured' if ELEVENLABS_API_KEY else 'not set (browser TTS fallback)'}")
    logger.info(f"Analytics:    {'key configured' if ANALYTICS_KEY else 'NO KEY — endpoint unprotected!'}")
    logger.info(f"Base URL:     {APP_BASE_URL}")
    logger.info(f"Database:     {DB_PATH}")

    # Fix #8: Cleanup expired data
    j, c, r = db.cleanup()
    if j or c or r:
        logger.info(f"Cleanup: {j} old jobs, {c} expired cache, {r} stale rate limits")

    # Fix #3: Recover orphaned jobs
    orphaned = db.jobs_get_orphaned(max_age_seconds=900)
    for job in orphaned:
        db.job_fail(job["job_id"], "Job orphaned after server restart")
        logger.warning(f"Marked orphaned job {job['job_id']} as failed")

    logger.info("=" * 50)

    yield  # App runs here

    # Shutdown
    if http_client:
        await http_client.aclose()


app = FastAPI(
    title="Du'a Generator API",
    description="Generate personalized Islamic supplications for any occasion.",
    version="1.3",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        APP_BASE_URL,
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Security Headers Middleware ──

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response as StarletteResponse


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        response: StarletteResponse = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if APP_ENV == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


app.add_middleware(SecurityHeadersMiddleware)


# ══════════════════════════════════════════════════
# Occasion Definitions
# ══════════════════════════════════════════════════

OCCASION_LABELS = {
    "ramadan": "the blessed last ten nights of Ramadan",
    "jumuah": "the blessed day of Jumu'ah (Friday)",
    "illness": "a time of illness — asking Allah for healing and ease",
    "travel": "an upcoming journey — asking Allah for safety and protection",
    "newborn": "the arrival of a new baby — asking Allah for blessings and protection",
    "marriage": "a marriage — asking Allah for love, mercy, and harmony",
    "grief": "a time of loss and grief — asking Allah for patience and comfort",
    "hajj": "the journey of Hajj or Umrah — asking Allah to accept the pilgrimage",
    "gratitude": "a moment of gratitude — thanking Allah for His blessings",
    "general": "everyday life — asking Allah for guidance, protection, and mercy",
}

OCCASION_DISPLAY = {
    "ramadan": "Ramadan — Last 10 Nights",
    "jumuah": "Jumu'ah (Friday)",
    "illness": "Illness / Healing",
    "travel": "Travel / Journey",
    "newborn": "New Baby",
    "marriage": "Marriage / Wedding",
    "grief": "Grief / Loss",
    "hajj": "Hajj / Umrah",
    "gratitude": "Gratitude / Shukr",
    "general": "General / Everyday",
}


# ══════════════════════════════════════════════════
# Prompt Builder
# ══════════════════════════════════════════════════

def build_prompt(user_name: str, members: list[FamilyMember], occasion: str = "general") -> str:
    member_count = len(members)

    # Detect solo user: only 1 member whose relationship is "Self" (or empty/unset)
    is_solo = (member_count == 1 and members[0].relationship.strip().lower() in ("self", "myself", "me", ""))

    length_instruction = get_length_instruction(member_count, is_solo=is_solo)
    occasion_label = OCCASION_LABELS.get(occasion, OCCASION_LABELS["general"])

    if is_solo:
        # ── Solo user prompt: personal du'a, no family member sections ──
        m = members[0]
        prompt = f"Please write a deeply personal du'a for {user_name}.\n\n"
        prompt += f"OCCASION: This du'a is for {occasion_label}.\n\n"
        prompt += f"LENGTH INSTRUCTION: {length_instruction}\n\n"

        prompt += "IMPORTANT STRUCTURE RULE: This du'a is for ONE person only. "
        prompt += "Do NOT create separate 'Individual Family Member' sections or '## A Du'a for [Name]' headers. "
        prompt += "Instead, write a single flowing supplication with:\n"
        prompt += "  1. Opening — Praise and glorify Allah\n"
        prompt += "  2. Personal supplication — Address the person's life, concerns, and blessings in a natural, flowing way\n"
        prompt += "  3. Closing — Forgiveness, mercy, occasion-appropriate references, and Ameen\n\n"

        prompt += "ABOUT THE PERSON:\n\n"
        prompt += f"   Name: {m.name}\n"
        prompt += f"   Age Range: {m.ageRange or 'Not specified'}\n"
        prompt += f"   Gender: {m.gender or 'Not specified'}\n"
        prompt += f"   Concerns/Prayer requests: {m.concerns or 'General well-being, faith, guidance, and mercy'}\n\n"

    else:
        # ── Family prompt: original structure with individual sections ──
        prompt = f"Please write a personalized du'a for {user_name} and their family.\n\n"
        prompt += f"OCCASION: This du'a is for {occasion_label}.\n\n"
        prompt += f"LENGTH INSTRUCTION: {length_instruction}\n\n"

        prompt += "FAMILY MEMBERS:\n\n"
        for m in members:
            prompt += f"   Name: {m.name}\n"
            prompt += f"   Relationship: {m.relationship or 'Not specified'}\n"
            prompt += f"   Age Range: {m.ageRange or 'Not specified'}\n"
            prompt += f"   Gender: {m.gender or 'Not specified'}\n"
            prompt += f"   Concerns/Prayer requests: {m.concerns or 'General well-being'}\n\n"

    return prompt


# ══════════════════════════════════════════════════
# AI Callers (with retry)
# ══════════════════════════════════════════════════

RETRYABLE_STATUS_CODES = {500, 502, 503, 529}  # Server errors + Anthropic overloaded
MAX_RETRIES = 2
RETRY_BACKOFF = 3  # seconds


async def call_openai(prompt: str, max_tokens: int = 8000) -> str:
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = await http_client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {OPENAI_API_KEY}"},
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
                    "max_tokens": max_tokens, "temperature": 0.8,
                },
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as e:
            last_error = e
            status = getattr(getattr(e, "response", None), "status_code", 0)
            if isinstance(e, httpx.HTTPStatusError) and status not in RETRYABLE_STATUS_CODES:
                raise  # Don't retry 400, 401, 429, etc.
            if attempt < MAX_RETRIES:
                logger.warning(f"OpenAI attempt {attempt + 1} failed ({type(e).__name__}), retrying in {RETRY_BACKOFF}s...")
                await asyncio.sleep(RETRY_BACKOFF * (attempt + 1))
            else:
                raise


async def call_anthropic(prompt: str, max_tokens: int = 8000) -> str:
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = await http_client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
                json={
                    "model": ANTHROPIC_MODEL, "max_tokens": max_tokens,
                    "cache_control": {"type": "ephemeral"},
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            response.raise_for_status()
            data = response.json()
            usage = data.get("usage", {})
            cache_read = usage.get("cache_read_input_tokens", 0)
            if cache_read > 0:
                logger.info(f"Cache HIT: {cache_read} tokens cached, {usage.get('output_tokens', 0)} output")
            return data["content"][0]["text"]
        except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as e:
            last_error = e
            status = getattr(getattr(e, "response", None), "status_code", 0)
            if isinstance(e, httpx.HTTPStatusError) and status not in RETRYABLE_STATUS_CODES:
                raise  # Don't retry 400, 401, 429, etc.
            if attempt < MAX_RETRIES:
                logger.warning(f"Anthropic attempt {attempt + 1} failed ({type(e).__name__}), retrying in {RETRY_BACKOFF}s...")
                await asyncio.sleep(RETRY_BACKOFF * (attempt + 1))
            else:
                raise


async def call_anthropic_batch(prompt: str, max_tokens: int, user_name: str, user_email: str) -> str:
    """Non-blocking batch submission. Returns job_id immediately."""
    request_id = uuid.uuid4().hex[:8]
    job_id = uuid.uuid4().hex[:12]

    batch_response = await http_client.post(
        "https://api.anthropic.com/v1/messages/batches",
        headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
        json={"requests": [{
            "custom_id": f"dua-{request_id}",
            "params": {
                "model": ANTHROPIC_MODEL, "max_tokens": max_tokens,
                "cache_control": {"type": "ephemeral"},
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
            },
        }]},
    )
    batch_response.raise_for_status()
    batch_id = batch_response.json()["id"]

    db.job_create(job_id, batch_id, request_id, user_name, user_email)
    logger.info(f"Batch job {job_id} created (batch: {batch_id})")
    return job_id


async def poll_batch_job(job_id: str):
    """
    Background task: polls Anthropic, stores result, sends email server-side.
    Fix #1: Email delivery is fully server-owned — no browser dependency.
    """
    job = db.job_get(job_id)
    if not job:
        logger.error(f"Job {job_id} not found for polling")
        return

    batch_id = job["batch_id"]
    request_id = job["request_id"]

    try:
        for attempt in range(40):  # 15s * 40 = 10 min max
            await asyncio.sleep(15)
            status_response = await http_client.get(
                f"https://api.anthropic.com/v1/messages/batches/{batch_id}",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
            )
            status_response.raise_for_status()
            status_data = status_response.json()
            if status_data.get("processing_status") == "ended":
                logger.info(f"Job {job_id} completed after {(attempt + 1) * 15}s")
                break
        else:
            db.job_fail(job_id, "Batch timed out after 10 minutes")
            return

        results_url = status_data.get("results_url", "")
        if not results_url:
            db.job_fail(job_id, "No results URL from Anthropic")
            return

        results_response = await http_client.get(
            results_url,
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
        )
        results_response.raise_for_status()

        dua_text = None
        for line in results_response.text.strip().split("\n"):
            if not line.strip():
                continue
            result = json.loads(line)
            if result.get("custom_id") == f"dua-{request_id}":
                content = result.get("result", {}).get("message", {}).get("content", [])
                if content and content[0].get("type") == "text":
                    dua_text = content[0]["text"]
                    break

        if not dua_text:
            db.job_fail(job_id, "Du'a not found in batch results")
            return

        db.job_complete(job_id, dua_text)

        # Server-owned email delivery with proper status tracking
        user_email = job.get("user_email", "")
        user_name = job.get("user_name", "")
        if user_email and SMTP_USERNAME:
            db.job_set_email_status(job_id, "sending")
            try:
                dua_id = uuid.uuid4().hex[:24]
                token = generate_email_token(dua_id)
                db.save_dua(dua_id, user_name, dua_text, "[]", token, private=True)
                # Fix #3: No "View online" link for private email-only du'as
                await send_dua_email(user_email, user_name, dua_text, share_url=None)
                db.job_set_email_status(job_id, "sent")
                db.track("emails_sent")
                logger.info(f"Job {job_id}: email sent to {user_email}")
            except Exception as e:
                db.job_set_email_status(job_id, "failed")
                logger.error(f"Job {job_id}: email send failed: {e}")
        else:
            db.job_set_email_status(job_id, "none")

    except Exception as e:
        db.job_fail(job_id, f"{type(e).__name__}: {str(e)[:200]}")
        logger.error(f"Job {job_id} failed: {e}")


async def generate_dua_text(prompt: str, member_count: int = 1, is_solo: bool = False,
                            delivery_mode: str = "instant",
                            user_name: str = "", user_email: str = "") -> str:
    max_tokens = get_max_tokens(member_count, is_solo=is_solo)
    if AI_PROVIDER == "anthropic":
        if not ANTHROPIC_API_KEY:
            raise HTTPException(500, "Anthropic API key not configured.")
        if delivery_mode == "email":
            job_id = await call_anthropic_batch(prompt, max_tokens, user_name, user_email)
            return f"__JOB__{job_id}"
        else:
            return await call_anthropic(prompt, max_tokens)
    else:
        if not OPENAI_API_KEY:
            raise HTTPException(500, "OpenAI API key not configured.")
        return await call_openai(prompt, max_tokens)


# ══════════════════════════════════════════════════
# Email Sender
# ══════════════════════════════════════════════════

def _markdown_to_html(text: str) -> str:
    """XSS-safe markdown to HTML conversion."""
    lines = text.split("\n")
    html_lines = []
    for line in lines:
        if line.startswith("## "):
            html_lines.append(f'<h2>{html_escape(line[3:])}</h2>')
        elif line.startswith("# "):
            html_lines.append(f'<h1 style="text-align:center;color:#8b6914;">{html_escape(line[2:])}</h1>')
        elif line.strip() == "---":
            html_lines.append('<hr style="border:none;border-top:1px solid #d4c4a0;margin:20px 0;">')
        elif line.strip() == "":
            html_lines.append("<br/>")
        else:
            safe = html_escape(line)
            safe = re.sub(r"\*\*\*(.+?)\*\*\*", r'<strong style="color:#8b6914;font-style:italic;">\1</strong>', safe)
            safe = re.sub(r"\*\*(.+?)\*\*", r'<strong style="color:#8b6914;">\1</strong>', safe)
            safe = re.sub(r"\*(.+?)\*", r'<em style="color:#6b5a3a;">\1</em>', safe)
            html_lines.append(f"<p>{safe}</p>")
    return "\n".join(html_lines)


async def send_dua_email(to_email: str, recipient_name: str, dua_text: str, share_url: Optional[str] = None):
    if not SMTP_USERNAME:
        raise HTTPException(500, "Email is not configured.")

    dua_html = _markdown_to_html(dua_text)

    # Fix #3: Only include "View online" link if share_url is provided (not for private/email-only du'as)
    footer_link = f'<a href="{share_url}" style="color:#8b6914;">View online</a><br/>' if share_url else ""

    html_body = f"""<html><body style="font-family:Georgia,serif;max-width:650px;margin:0 auto;padding:20px;color:#2c2c2c;line-height:1.8;background:#faf6ef;">
<div style="text-align:center;padding:20px 0;border-bottom:1px solid #d4c4a0;margin-bottom:20px;">
  <div style="font-size:24px;color:#8b6914;font-weight:bold;">Your Personalized Du'a</div>
  <div style="font-size:14px;color:#888;margin-top:4px;">A heartfelt supplication for {html_escape(recipient_name)} and family</div>
</div>
{dua_html}
<div style="text-align:center;margin-top:30px;padding-top:20px;border-top:1px solid #d4c4a0;">
  {footer_link}
  <p style="font-size:11px;color:#888;margin-top:10px;">Generated at mydua.ai — support@mydua.ai</p>
</div></body></html>"""

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>"
    msg["To"] = to_email
    msg["Subject"] = f"Your Du'a — {recipient_name}"
    msg.attach(MIMEText(dua_text, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    await aiosmtplib.send(msg, hostname=SMTP_HOST, port=SMTP_PORT,
                          username=SMTP_USERNAME, password=SMTP_PASSWORD, start_tls=True)


# ══════════════════════════════════════════════════
# API Routes
# ══════════════════════════════════════════════════

@app.get("/api/health")
async def health_check():
    checks = {
        "status": "ok",
        "provider": AI_PROVIDER,
        "version": "1.3",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    # Verify database is reachable
    try:
        db.get_stats()
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "error"
        checks["status"] = "degraded"

    # Verify AI provider is configured
    if AI_PROVIDER == "anthropic" and not ANTHROPIC_API_KEY:
        checks["ai_provider"] = "not configured"
        checks["status"] = "degraded"
    elif AI_PROVIDER != "anthropic" and not OPENAI_API_KEY:
        checks["ai_provider"] = "not configured"
        checks["status"] = "degraded"
    else:
        checks["ai_provider"] = "configured"

    # Verify email is configured
    if SMTP_USERNAME and SMTP_PASSWORD and SMTP_FROM_EMAIL:
        checks["email"] = "configured"
        checks["email_from"] = SMTP_FROM_EMAIL
    elif SMTP_USERNAME and not SMTP_PASSWORD:
        checks["email"] = "missing SMTP_PASSWORD"
        checks["status"] = "degraded"
    elif not SMTP_USERNAME:
        checks["email"] = "not configured (SMTP_USERNAME missing)"
    else:
        checks["email"] = "incomplete config"

    return checks


@app.post("/api/generate-dua")
async def generate_dua(req: GenerateDuaRequest, request: Request, background_tasks: BackgroundTasks):
    if not req.userName.strip():
        raise HTTPException(400, "Please enter your name.")

    valid_members = [m for m in req.members if m.name.strip()]
    if not valid_members:
        raise HTTPException(400, "Please add at least one family member with a name.")

    # Fix #4: SQLite-backed rate limiting
    # Fix #4: Safe client IP extraction
    client_ip = get_client_ip(request)
    allowed, remaining = db.rate_limit_check(f"gen:{client_ip}", max_requests=5, window_seconds=3600)
    if not allowed:
        raise HTTPException(429, f"Rate limit exceeded. Please try again later.")

    # Check cache
    if not req.skipCache:
        cache_key = db.make_cache_key(req.userName, [m.model_dump() for m in valid_members])
        cached = db.cache_get(cache_key)
        if cached:
            db.track("duas_generated")
            if req.userEmail:
                db.track_email(req.userEmail, req.userName)
            return {"dua": cached, "cached": True}

    prompt = build_prompt(req.userName, valid_members, req.occasion)

    # Detect solo user for token/length optimization
    is_solo = (len(valid_members) == 1 and valid_members[0].relationship.strip().lower() in ("self", "myself", "me", ""))

    try:
        # Fix #7: Email comes from validated request field, not header
        dua_text = await generate_dua_text(
            prompt, member_count=len(valid_members), is_solo=is_solo,
            delivery_mode=req.deliveryMode,
            user_name=req.userName,
            user_email=req.userEmail or "",
        )
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        detail = e.response.text[:200]
        logger.error(f"AI API error {status}: {detail}")
        if status == 401:
            raise HTTPException(502, "AI API key is invalid or expired.")
        elif status == 429:
            raise HTTPException(502, "AI API rate limit exceeded. Please wait and try again.")
        elif status == 400:
            raise HTTPException(502, f"AI API rejected request: {detail[:100]}")
        else:
            raise HTTPException(502, f"AI service error {status}.")
    except httpx.ConnectError:
        raise HTTPException(502, "Cannot connect to AI service. Check server internet.")
    except httpx.TimeoutException:
        raise HTTPException(504, "AI service timed out. Try fewer family members.")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Generate error: {type(e).__name__}: {e}")
        raise HTTPException(500, f"Generation failed: {type(e).__name__}")

    if not dua_text:
        raise HTTPException(502, "Empty response from AI.")

    # Batch mode returns job marker
    if dua_text.startswith("__JOB__"):
        job_id = dua_text[7:]
        background_tasks.add_task(poll_batch_job, job_id)
        db.track("duas_generated")
        return {"jobId": job_id, "status": "processing", "cached": False}

    # Instant mode
    cache_key = db.make_cache_key(req.userName, [m.model_dump() for m in valid_members])
    db.cache_put(cache_key, dua_text)
    db.track("duas_generated")
    if req.userEmail:
        db.track_email(req.userEmail, req.userName)
    db.log_event("dua_generated", detail=f"members:{len(valid_members)}", ip=client_ip,
                 user_agent=request.headers.get("user-agent", ""),
                 referrer=request.headers.get("referer", ""))
    if req.referred:
        db.track("referred_generations")
    return {"dua": dua_text, "cached": False}


@app.get("/api/job/{job_id}")
async def get_job_status(job_id: str):
    job = db.job_get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    response = {"jobId": job["job_id"], "status": job["status"],
                "emailStatus": job.get("email_status", "none")}
    if job["status"] == "completed":
        response["dua"] = job["dua"]
    elif job["status"] == "failed":
        response["error"] = job.get("error", "Unknown error")
    return response


@app.post("/api/save-dua")
async def save_dua(req: SaveDuaRequest, request: Request):
    client_ip = get_client_ip(request)
    allowed, _ = db.rate_limit_check(f"save:{client_ip}", max_requests=10, window_seconds=3600)
    if not allowed:
        raise HTTPException(429, "Too many save requests.")

    dua_id = uuid.uuid4().hex[:24]
    token = generate_email_token(dua_id)
    members_json = json.dumps([m.model_dump() for m in req.members]) if req.members else "[]"
    db.save_dua(dua_id, req.userName, req.dua, members_json, token)
    db.track("shares_created")

    return {
        "id": dua_id,
        "shareUrl": f"{APP_BASE_URL}/shared/{dua_id}?ref=share",
        "emailToken": token,  # Fix #5: Token returned for authorized email sends
    }


@app.get("/api/saved/{dua_id}")
async def get_saved_dua(dua_id: str):
    data = db.get_saved(dua_id)
    if not data:
        raise HTTPException(404, "Du'a not found.")
    # Fix #2: Private du'as (email-only) are not accessible via API
    if data.get("private"):
        raise HTTPException(404, "This du'a is private.")
    return {
        "id": data["dua_id"],
        "userName": data["user_name"],
        "dua": data["dua"],
        "created": data["created"],
    }


@app.post("/api/email-dua")
async def email_dua(req: EmailDuaRequest, request: Request):
    # Fix #4: SQLite rate limit
    client_ip = get_client_ip(request)
    allowed, _ = db.rate_limit_check(f"email:{client_ip}", max_requests=5, window_seconds=3600)
    if not allowed:
        raise HTTPException(429, "Too many email requests.")

    data = db.get_saved(req.duaId)
    if not data:
        raise HTTPException(404, "Du'a not found. Save it first.")

    # Fix #5: Verify HMAC token
    if not req.token or not verify_email_token(req.duaId, req.token):
        raise HTTPException(403, "Invalid email authorization token.")

    share_url = f"{APP_BASE_URL}/shared/{req.duaId}?ref=share"
    try:
        await send_dua_email(req.email, req.recipientName or data["user_name"], data["dua"], share_url)
    except aiosmtplib.SMTPAuthenticationError as e:
        logger.error(f"Email auth failed: {e}")
        raise HTTPException(500, "Email authentication failed. Check SMTP_USERNAME and SMTP_PASSWORD (Gmail requires an App Password, not your account password).")
    except aiosmtplib.SMTPConnectError as e:
        logger.error(f"Email connection failed: {e}")
        raise HTTPException(500, "Cannot connect to email server. Check SMTP_HOST and SMTP_PORT.")
    except aiosmtplib.SMTPRecipientsRefused as e:
        logger.error(f"Email recipient refused: {e}")
        raise HTTPException(500, "Email address was rejected by the mail server.")
    except Exception as e:
        logger.error(f"Email failed ({type(e).__name__}): {e}")
        raise HTTPException(500, f"Failed to send email: {type(e).__name__}")

    db.track("emails_sent")
    return {"status": "sent", "to": req.email}


@app.post("/api/track-pdf")
async def track_pdf(request: Request):
    client_ip = get_client_ip(request)
    allowed, _ = db.rate_limit_check(f"track:{client_ip}", max_requests=30, window_seconds=3600)
    if not allowed:
        return {"status": "rate_limited"}
    db.track("pdfs_exported")
    return {"status": "tracked"}


@app.post("/api/track-sms")
async def track_sms(request: Request):
    client_ip = get_client_ip(request)
    allowed, _ = db.rate_limit_check(f"track:{client_ip}", max_requests=30, window_seconds=3600)
    if not allowed:
        return {"status": "rate_limited"}
    db.track("sms_shares")
    return {"status": "tracked"}


@app.post("/api/track-form-start")
async def track_form_start(request: Request):
    """Tracks when a user starts interacting with the form. Bounce rate = page_views - form_started."""
    client_ip = get_client_ip(request)
    allowed, _ = db.rate_limit_check(f"track:{client_ip}", max_requests=30, window_seconds=3600)
    if not allowed:
        return {"status": "rate_limited"}
    db.track("form_started")
    return {"status": "tracked"}


@app.post("/api/track-referral")
async def track_referral(request: Request):
    """Tracks when a user arrives via a shared link (?ref=share)."""
    client_ip = get_client_ip(request)
    allowed, _ = db.rate_limit_check(f"track:{client_ip}", max_requests=30, window_seconds=3600)
    if not allowed:
        return {"status": "rate_limited"}
    db.track("referred_visits")
    db.log_event("referred_visit", ip=get_client_ip(request),
                 user_agent=request.headers.get("user-agent", ""),
                 referrer=request.headers.get("referer", ""))
    return {"status": "tracked"}


@app.post("/api/track-pageview")
async def track_pageview(request: Request):
    client_ip = get_client_ip(request)
    allowed, _ = db.rate_limit_check(f"track:{client_ip}", max_requests=30, window_seconds=3600)
    if not allowed:
        return {"status": "rate_limited"}
    db.track("page_views")
    db.log_event("page_view", ip=get_client_ip(request),
                 user_agent=request.headers.get("user-agent", ""),
                 referrer=request.headers.get("referer", ""))
    return {"status": "tracked"}


@app.get("/api/analytics")
async def get_analytics(request: Request):
    # Auth: require ANALYTICS_KEY via query param or Authorization header
    key = request.query_params.get("key", "")
    if not key:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            key = auth_header[7:]
    if not ANALYTICS_KEY or not hmac.compare_digest(key, ANALYTICS_KEY):
        raise HTTPException(401, "Unauthorized. Provide ?key= or Authorization: Bearer <key>.")

    stats = db.get_stats()
    stats["total_email_subscribers"] = db.get_email_count()

    # Computed metrics
    pv = stats.get("total_page_views", 0)
    fs = stats.get("total_form_started", 0)
    dg = stats.get("total_duas_generated", 0)
    rv = stats.get("total_referred_visits", 0)
    rg = stats.get("total_referred_generations", 0)

    # Bounce rate: visitors who viewed the page but never interacted with the form
    stats["bounce_rate"] = round((1 - fs / pv) * 100, 1) if pv > 0 else 0
    # Form-to-generate conversion: users who started the form and completed a du'a
    stats["form_conversion_rate"] = round((dg / fs) * 100, 1) if fs > 0 else 0
    # Share-to-conversion: referred visitors who generated their own du'a
    stats["share_conversion_rate"] = round((rg / rv) * 100, 1) if rv > 0 else 0

    return stats


@app.get("/shared/{dua_id}", response_class=HTMLResponse)
async def shared_page(dua_id: str):
    data = db.get_saved(dua_id)
    if not data:
        raise HTTPException(404, "Du'a not found.")

    # Fix #6: Private du'as (from email-only delivery) are not publicly viewable
    if data.get("private"):
        raise HTTPException(404, "This du'a was delivered privately via email.")

    user_name = html_escape(data["user_name"])
    dua_html = _markdown_to_html(data["dua"])

    return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>Du'a for {user_name} — mydua.ai</title>
  <link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;0,700;1,400;1,700&family=Amiri:wght@400;700&display=swap" rel="stylesheet"/>
  <style>
    *{{margin:0;padding:0;box-sizing:border-box;}}
    body{{font-family:'Cormorant Garamond',serif;background:linear-gradient(160deg,#0a1628 0%,#112240 40%,#0d1f3c 70%,#091525 100%);color:#e8dcc8;min-height:100vh;padding:40px 20px;line-height:1.8;}}
    .wrap{{max-width:700px;margin:0 auto;}}
    .header{{text-align:center;margin-bottom:30px;padding-bottom:20px;border-bottom:1px solid rgba(201,169,110,.2);}}
    .bismillah{{font-family:'Amiri',serif;font-size:28px;color:#c9a96e;direction:rtl;opacity:.8;}}
    h1{{font-size:28px;font-weight:300;color:#e8dcc8;margin:10px 0;}}
    .sub{{font-size:14px;color:#8a7d6b;font-style:italic;}}
    h2{{font-size:22px;color:#c9a96e;margin:28px 0 12px;}}
    p{{font-size:16px;margin:4px 0;}}
    strong{{color:#c9a96e;}}em{{color:#b8a88a;}}
    hr{{border:none;border-top:1px solid rgba(201,169,110,.2);margin:20px 0;}}
    .footer{{text-align:center;font-size:12px;color:#8a7d6b;margin-top:30px;padding-top:20px;border-top:1px solid rgba(201,169,110,.2);}}
    .footer a{{color:#c9a96e;text-decoration:none;}}
  </style>
</head>
<body><div class="wrap">
  <div class="header">
    <div class="bismillah">بِسْمِ اللَّهِ الرَّحْمَنِ الرَّحِيمِ</div>
    <h1>A Du'a for {user_name}</h1>
    <div class="sub">A personalized supplication from mydua.ai</div>
  </div>
  {dua_html}
  <div class="footer">
    <a href="/?ref=share">Generate your own du'a at mydua.ai</a>
    <p style="margin-top:10px;">Please verify all Quranic verses and Hadith with authentic Islamic sources.</p>
  </div>
</div></body></html>""")


# ══════════════════════════════════════════════════
# Text-to-Speech (Listen to Du'a)
# ══════════════════════════════════════════════════

class TTSRequest(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def text_limit(cls, v):
        if len(v) > 15000:
            raise ValueError("Text too long for speech synthesis")
        return v


@app.post("/api/tts")
async def text_to_speech(req: TTSRequest):
    """
    Convert du'a text to speech audio.
    Uses ElevenLabs if configured (premium quality), returns 501 otherwise
    (frontend falls back to browser Speech Synthesis).
    """
    if not ELEVENLABS_API_KEY:
        raise HTTPException(501, "TTS not configured")

    try:
        response = await http_client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json={
                "text": req.text,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {
                    "stability": 0.65,         # Slightly varied for natural feel
                    "similarity_boost": 0.75,
                    "style": 0.4,              # Moderate expressiveness
                    "use_speaker_boost": True,
                }
            },
            timeout=60,
        )
        response.raise_for_status()

        from fastapi.responses import Response
        return Response(
            content=response.content,
            media_type="audio/mpeg",
            headers={"Content-Disposition": "inline; filename=dua-reading.mp3"}
        )

    except httpx.HTTPStatusError as e:
        logger.error(f"ElevenLabs error: {e.response.status_code}")
        raise HTTPException(502, "Voice generation failed")
    except Exception as e:
        logger.error(f"TTS error: {e}")
        raise HTTPException(500, "Voice generation failed")


# ══════════════════════════════════════════════════
# Gift Du'a System
# ══════════════════════════════════════════════════

GIFT_OCCASIONS = {
    "ramadan": "the blessed last nights of Ramadan",
    "illness": "a time of illness, asking Allah for healing",
    "newborn": "the arrival of a new blessing in their family",
    "hajj": "their journey for Hajj",
    "wedding": "the celebration of their marriage",
    "hardship": "a difficult time, asking Allah for ease",
    "general": "care and love",
}


async def send_gift_email(to_email: str, sender_name: str, recipient_name: str,
                          dua_text: str, personal_message: str, gift_id: str):
    """Send a gift du'a via email (free tier)."""
    dua_html = _markdown_to_html(dua_text)
    msg_html = f"<p style='color:#6b5a3a;font-style:italic;border-left:3px solid #c9a96e;padding-left:16px;margin:16px 0;'>{html_escape(personal_message)}</p>" if personal_message else ""
    view_url = f"{APP_BASE_URL}/gift/{gift_id}"

    html_body = f"""<html><body style="font-family:Georgia,serif;max-width:650px;margin:0 auto;padding:20px;color:#2c2c2c;line-height:1.8;background:#faf6ef;">
<div style="text-align:center;padding:20px 0;border-bottom:1px solid #d4c4a0;margin-bottom:20px;">
  <div style="font-size:14px;color:#888;">A du'a was made for you by</div>
  <div style="font-size:26px;color:#8b6914;font-weight:bold;margin:8px 0;">{html_escape(sender_name)}</div>
  <div style="font-size:14px;color:#888;">with love and du'a</div>
</div>
{msg_html}
{dua_html}
<div style="text-align:center;margin-top:30px;padding-top:20px;border-top:1px solid #d4c4a0;">
  <a href="{view_url}" style="display:inline-block;padding:12px 28px;background:#8b6914;color:#fff;text-decoration:none;border-radius:8px;font-weight:bold;">View Your Du'a</a>
  <p style="margin-top:16px;font-size:13px;color:#888;">Want to make a du'a for someone you love?</p>
  <a href="{APP_BASE_URL}" style="color:#8b6914;font-size:13px;">Visit mydua.ai</a>
  <p style="font-size:11px;color:#aaa;margin-top:16px;">Generated at mydua.ai — support@mydua.ai</p>
</div></body></html>"""

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>"
    msg["To"] = to_email
    msg["Subject"] = f"{sender_name} made a du'a for you, {recipient_name}"
    msg.attach(MIMEText(f"{sender_name} made a du'a for you:\n\n{dua_text}\n\nView online: {view_url}", "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    await aiosmtplib.send(msg, hostname=SMTP_HOST, port=SMTP_PORT,
                          username=SMTP_USERNAME, password=SMTP_PASSWORD, start_tls=True)


async def send_gift_sms(to_phone: str, sender_name: str, recipient_name: str, gift_id: str):
    """Send a gift du'a notification via SMS (Twilio)."""
    view_url = f"{APP_BASE_URL}/gift/{gift_id}"
    body = (
        f"Assalamu alaikum {recipient_name}, a du'a was made for you by {sender_name}.\n\n"
        f"View your du'a: {view_url}\n\n— mydua.ai"
    )
    response = await http_client.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json",
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        data={"From": TWILIO_FROM_NUMBER, "To": to_phone, "Body": body},
    )
    response.raise_for_status()
    return response.json().get("sid")


async def send_gift_postcard(gift: dict):
    """Send a gift du'a as a physical postcard (Lob)."""
    address = json.loads(gift.get("postcard_address_json", "{}"))
    if not address:
        raise ValueError("No postcard address provided")

    # Build the postcard HTML (Lob renders this)
    dua_preview = gift["dua"][:600] + ("..." if len(gift["dua"]) > 600 else "")
    back_html = f"""<html><body style="font-family:Georgia,serif;padding:20px;font-size:11px;color:#2c2c2c;line-height:1.5;">
<div style="text-align:center;margin-bottom:12px;">
  <div style="font-size:16px;color:#8b6914;font-weight:bold;">A Du'a Was Made For You</div>
  <div style="font-size:11px;color:#888;margin-top:4px;">by {html_escape(gift['sender_name'])}</div>
</div>
<div style="font-size:10px;">{html_escape(dua_preview)}</div>
<div style="text-align:center;margin-top:12px;font-size:10px;color:#8b6914;">
  Read the full du'a at mydua.ai/gift/{gift['gift_id']}
</div>
</body></html>"""

    front_html = f"""<html><body style="font-family:Georgia,serif;display:flex;align-items:center;justify-content:center;min-height:100%;background:linear-gradient(135deg,#faf6ef,#f0e8d0);text-align:center;padding:40px;">
<div>
  <div style="font-size:28px;color:#8b6914;font-weight:bold;margin-bottom:8px;">بِسْمِ اللَّهِ</div>
  <div style="font-size:18px;color:#2c2c2c;">A Du'a for {html_escape(gift['recipient_name'])}</div>
  <div style="font-size:12px;color:#888;margin-top:8px;">Made with love by {html_escape(gift['sender_name'])}</div>
  <div style="font-size:11px;color:#8b6914;margin-top:16px;">mydua.ai</div>
</div>
</body></html>"""

    response = await http_client.post(
        "https://api.lob.com/v1/postcards",
        auth=(LOB_API_KEY, ""),
        json={
            "to": {
                "name": gift["recipient_name"],
                "address_line1": address.get("line1", ""),
                "address_line2": address.get("line2", ""),
                "address_city": address.get("city", ""),
                "address_state": address.get("state", ""),
                "address_zip": address.get("zip", ""),
                "address_country": address.get("country", "US"),
            },
            "from": {
                "name": LOB_FROM_NAME,
                "address_line1": LOB_FROM_ADDRESS,
                "address_city": LOB_FROM_CITY,
                "address_state": LOB_FROM_STATE,
                "address_zip": LOB_FROM_ZIP,
                "address_country": "US",
            },
            "front": front_html,
            "back": back_html,
            "size": "4x6",
        },
    )
    response.raise_for_status()
    return response.json().get("id")


# ── Gift API Endpoints ──

@app.post("/api/gift/generate")
async def generate_gift_dua(req: GiftDuaRequest, request: Request):
    """Generate a personalized gift du'a for a recipient."""
    client_ip = get_client_ip(request)
    allowed, _ = db.rate_limit_check(f"gen:{client_ip}", max_requests=5, window_seconds=3600)
    if not allowed:
        raise HTTPException(429, "Rate limit exceeded. Please try again later.")

    occasion = GIFT_OCCASIONS.get(req.occasion, "care and love")

    # Build a gift-specific prompt
    prompt = (
        f"Please write a personalized du'a as a gift.\n\n"
        f"This du'a is FROM {req.senderName} TO {req.recipientName}.\n"
        f"Relationship: {req.recipientRelationship or 'a loved one'}\n"
        f"Occasion: {occasion}\n"
        f"Age range: {req.recipientAgeRange or 'Not specified'}\n"
        f"Gender: {req.recipientGender or 'Not specified'}\n"
        f"Concerns/prayer requests: {req.concerns or 'General well-being and blessings'}\n\n"
        f"LENGTH INSTRUCTION: {get_length_instruction(2)}\n\n"
        f"IMPORTANT: Write this du'a as if {req.senderName} is making du'a FOR {req.recipientName}. "
        f"Use second-person address to Allah ('O Allah, bless {req.recipientName}...'). "
        f"The tone should convey deep love and care from the sender to the recipient.\n\n"
        f"Do NOT include Arabic script or transliteration — English only.\n"
    )

    try:
        dua_text = await generate_dua_text(prompt, member_count=2, delivery_mode="instant",
                                           user_name=req.senderName)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Gift generation error: {e}")
        raise HTTPException(500, "Failed to generate gift du'a.")

    if not dua_text or dua_text.startswith("__JOB__"):
        raise HTTPException(502, "Failed to generate du'a.")

    # Store the gift
    gift_id = uuid.uuid4().hex[:24]
    context = json.dumps({
        "occasion": req.occasion,
        "personalMessage": req.personalMessage,
        "recipientRelationship": req.recipientRelationship,
    })
    db.gift_create(gift_id, req.senderName, req.senderEmail, req.recipientName,
                   "", "pending", dua_text, context)
    db.track("duas_generated")

    return {
        "giftId": gift_id,
        "dua": dua_text,
        "senderName": req.senderName,
        "recipientName": req.recipientName,
    }


@app.post("/api/gift/deliver")
async def deliver_gift(req: GiftDeliverRequest, request: Request):
    """Deliver a gift du'a via email (free), SMS ($2.49), or postcard ($10.99)."""
    gift = db.gift_get(req.giftId)
    if not gift:
        raise HTTPException(404, "Gift not found.")

    client_ip = get_client_ip(request)
    context = json.loads(gift.get("dua_context", "{}"))
    personal_message = context.get("personalMessage", "")

    if req.method == "email":
        # Free tier — send immediately
        if not req.recipientEmail:
            raise HTTPException(400, "Recipient email is required.")
        allowed, _ = db.rate_limit_check(f"email:{client_ip}", max_requests=5, window_seconds=3600)
        if not allowed:
            raise HTTPException(429, "Too many email requests.")

        try:
            await send_gift_email(req.recipientEmail, gift["sender_name"],
                                  gift["recipient_name"], gift["dua"],
                                  personal_message, req.giftId)
            # Bug #6: Update both status AND delivery_method
            db.gift_update(req.giftId, delivery_status="sent", delivery_method="email",
                           recipient_contact=req.recipientEmail)
            db.track("gifts_email")
            return {"status": "sent", "method": "email"}
        except Exception as e:
            logger.error(f"Gift email failed: {e}")
            db.gift_set_status(req.giftId, "failed")
            raise HTTPException(500, "Failed to send email.")

    elif req.method == "sms":
        # Premium — $2.49 via Stripe Checkout
        if not req.recipientPhone:
            raise HTTPException(400, "Recipient phone number is required.")
        if not STRIPE_SECRET_KEY:
            raise HTTPException(500, "Payments not configured.")
        # Bug #3: Verify Twilio is configured before accepting payment
        if not TWILIO_ACCOUNT_SID:
            raise HTTPException(503, "SMS delivery is not currently available.")

        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": f"Gift Du'a SMS to {gift['recipient_name']}",
                        "description": f"A personalized du'a from {gift['sender_name']}",
                    },
                    "unit_amount": GIFT_SMS_PRICE,
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=f"{APP_BASE_URL}/gift/paid?session_id={{CHECKOUT_SESSION_ID}}&gift_id={req.giftId}",
            cancel_url=f"{APP_BASE_URL}/gift.html",
            metadata={"gift_id": req.giftId, "method": "sms", "phone": req.recipientPhone},
        )
        # Store phone for delivery after payment
        db.gift_update(req.giftId, recipient_contact=req.recipientPhone,
                        delivery_method="sms", stripe_session_id=session.id)
        return {"status": "checkout", "url": session.url}

    elif req.method == "postcard":
        # Premium — $10.99 via Stripe Checkout
        if not req.postcardAddress:
            raise HTTPException(400, "Mailing address is required.")
        if not STRIPE_SECRET_KEY:
            raise HTTPException(500, "Payments not configured.")
        # Bug #3: Verify Lob is configured before accepting payment
        if not LOB_API_KEY:
            raise HTTPException(503, "Postcard delivery is not currently available.")

        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": f"Gift Du'a Postcard to {gift['recipient_name']}",
                        "description": f"A personalized du'a from {gift['sender_name']}, printed and mailed worldwide",
                    },
                    "unit_amount": GIFT_POSTCARD_PRICE,
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=f"{APP_BASE_URL}/gift/paid?session_id={{CHECKOUT_SESSION_ID}}&gift_id={req.giftId}",
            cancel_url=f"{APP_BASE_URL}/gift.html",
            metadata={"gift_id": req.giftId, "method": "postcard"},
        )
        addr_json = req.postcardAddress.model_dump_json()
        db.gift_update(req.giftId, delivery_method="postcard",
                        postcard_address_json=addr_json, stripe_session_id=session.id)
        return {"status": "checkout", "url": session.url}


# Bug #3 fix: /api/gift/fulfill removed entirely — fulfillment only happens
# through /gift/paid → fulfill_gift_background. No public endpoint for side effects.


@app.get("/gift/paid", response_class=HTMLResponse)
async def gift_paid_page(session_id: str, gift_id: str, background_tasks: BackgroundTasks):
    """Post-payment page — triggers delivery and shows confirmation."""
    gift = db.gift_get(gift_id)
    if not gift:
        raise HTTPException(404, "Gift not found.")

    # Bug #1 fix: Verify the Stripe session belongs to THIS gift
    if gift.get("stripe_session_id") and gift["stripe_session_id"] != session_id:
        raise HTTPException(403, "Payment session does not match this gift.")

    # Bug #2 fix: Idempotency — only trigger fulfillment if still pending
    if gift["delivery_status"] in ("sent", "sending"):
        # Already fulfilled or in progress — just show the confirmation page
        logger.info(f"Gift {gift_id} already {gift['delivery_status']}, skipping re-fulfillment")
    elif gift["delivery_status"] == "pending":
        # Mark as "sending" immediately to prevent duplicate triggers on refresh
        db.gift_set_status(gift_id, "sending")
        background_tasks.add_task(fulfill_gift_background, session_id, gift_id)
    # If "failed", allow retry
    elif gift["delivery_status"] == "failed":
        db.gift_set_status(gift_id, "sending")
        background_tasks.add_task(fulfill_gift_background, session_id, gift_id)

    method = gift["delivery_method"]
    recipient = html_escape(gift["recipient_name"])
    method_detail = "an SMS will be sent shortly" if method == "sms" else "a postcard will be printed and mailed within 3-5 business days"

    return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="en"><head>
  <meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>Gift Sent — mydua.ai</title>
  <link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@300;400;600;700&family=Amiri:wght@400;700&display=swap" rel="stylesheet"/>
  <style>*{{margin:0;padding:0;box-sizing:border-box;}}body{{font-family:'Cormorant Garamond',serif;background:linear-gradient(160deg,#0a1628,#112240,#091525);color:#e8dcc8;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:40px 20px;}}.card{{max-width:520px;background:rgba(255,255,255,.03);border:1px solid rgba(201,169,110,.15);border-radius:20px;padding:48px 40px;text-align:center;}}h1{{font-size:28px;font-weight:300;margin-bottom:12px;}}.gold{{color:#c9a96e;font-weight:600;}}p{{font-size:16px;color:#d4c9b4;line-height:1.7;margin-bottom:12px;}}a{{display:inline-block;margin-top:20px;padding:12px 32px;border:1px solid rgba(201,169,110,.3);border-radius:10px;color:#c9a96e;text-decoration:none;font-weight:600;}}</style>
</head><body><div class="card">
  <div style="font-family:'Amiri',serif;font-size:28px;color:#c9a96e;direction:rtl;opacity:.8;margin-bottom:16px;">جَزَاكَ اللَّهُ خَيْرًا</div>
  <h1><span class="gold">Gift Sent!</span></h1>
  <p>Your du'a gift for <strong style="color:#c9a96e;">{recipient}</strong> is on its way — {method_detail}.</p>
  <p>May Allah reward you for this beautiful act of love.</p>
  <a href="/gift.html">Send another gift</a>
</div></body></html>""")


async def fulfill_gift_background(session_id: str, gift_id: str):
    """Background task to fulfill a paid gift after Stripe payment."""
    try:
        # Re-check status in case of race condition
        gift = db.gift_get(gift_id)
        if not gift:
            return
        if gift["delivery_status"] == "sent":
            logger.info(f"Gift {gift_id} already sent, skipping duplicate fulfillment")
            return

        session = stripe.checkout.Session.retrieve(session_id)
        if session.payment_status != "paid":
            db.gift_set_status(gift_id, "payment_failed")
            return

        # Verify session belongs to this gift
        if gift.get("stripe_session_id") and gift["stripe_session_id"] != session_id:
            db.gift_set_status(gift_id, "failed")
            logger.error(f"Gift {gift_id}: session mismatch in background fulfillment")
            return

        method = gift["delivery_method"]
        if method == "sms" and TWILIO_ACCOUNT_SID:
            await send_gift_sms(gift["recipient_contact"], gift["sender_name"],
                                gift["recipient_name"], gift_id)
            db.gift_set_status(gift_id, "sent")
            db.track("gifts_sms")
        elif method == "postcard" and LOB_API_KEY:
            await send_gift_postcard(gift)
            db.gift_set_status(gift_id, "sent")
            db.track("gifts_postcard")
        else:
            db.gift_set_status(gift_id, "failed")
            logger.error(f"Gift {gift_id}: delivery service not configured for {method}")

    except Exception as e:
        db.gift_set_status(gift_id, "failed")
        logger.error(f"Gift fulfillment failed {gift_id}: {e}")


@app.get("/gift/{gift_id}", response_class=HTMLResponse)
async def view_gift(gift_id: str):
    """Public gift viewing page — the recipient sees this."""
    gift = db.gift_get(gift_id)
    if not gift:
        raise HTTPException(404, "Gift not found.")

    sender = html_escape(gift["sender_name"])
    recipient = html_escape(gift["recipient_name"])
    context = json.loads(gift.get("dua_context", "{}"))
    personal_msg = html_escape(context.get("personalMessage", ""))
    dua_html = _markdown_to_html(gift["dua"])
    msg_block = f'<div style="border-left:3px solid #c9a96e;padding-left:16px;margin:20px 0;font-style:italic;color:#b8a88a;">"{personal_msg}"<br/><span style="color:#c9a96e;">— {sender}</span></div>' if personal_msg else ""

    return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>A Du'a for {recipient} — mydua.ai</title>
  <meta property="og:title" content="A du'a was made for {recipient}"/>
  <meta property="og:description" content="{sender} made a heartfelt du'a for you."/>
  <link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;0,700;1,400;1,700&family=Amiri:wght@400;700&display=swap" rel="stylesheet"/>
  <style>
    *{{margin:0;padding:0;box-sizing:border-box;}}
    body{{font-family:'Cormorant Garamond',serif;background:linear-gradient(160deg,#0a1628 0%,#112240 40%,#0d1f3c 70%,#091525 100%);color:#e8dcc8;min-height:100vh;padding:40px 20px;line-height:1.8;}}
    .wrap{{max-width:700px;margin:0 auto;}}
    .header{{text-align:center;margin-bottom:30px;padding-bottom:20px;border-bottom:1px solid rgba(201,169,110,.2);}}
    .from{{font-size:15px;color:#8a7d6b;}}
    .sender{{font-size:26px;color:#c9a96e;font-weight:600;margin:6px 0;}}
    .for{{font-size:20px;color:#e8dcc8;font-weight:300;}}
    h2{{font-size:22px;color:#c9a96e;margin:28px 0 12px;}}
    p{{font-size:16px;margin:4px 0;}}
    strong{{color:#c9a96e;}}em{{color:#b8a88a;}}
    hr{{border:none;border-top:1px solid rgba(201,169,110,.2);margin:20px 0;}}
    .footer{{text-align:center;font-size:13px;color:#8a7d6b;margin-top:30px;padding-top:20px;border-top:1px solid rgba(201,169,110,.2);}}
    .footer a{{color:#c9a96e;text-decoration:none;}}
    .cta{{display:inline-block;margin-top:16px;padding:14px 32px;background:linear-gradient(135deg,#c9a96e,#a8873f);color:#0a1628;border-radius:10px;text-decoration:none;font-weight:700;font-size:16px;}}
  </style>
</head>
<body><div class="wrap">
  <div class="header">
    <div style="font-family:'Amiri',serif;font-size:28px;color:#c9a96e;direction:rtl;opacity:.8;margin-bottom:12px;">بِسْمِ اللَّهِ الرَّحْمَنِ الرَّحِيمِ</div>
    <div class="from">A du'a was made for you by</div>
    <div class="sender">{sender}</div>
    <div class="for">for {recipient}</div>
  </div>
  {msg_block}
  {dua_html}
  <div class="footer">
    <p>May Allah accept this du'a.</p>
    <a href="/?ref=share" class="cta">Make a du'a for someone you love</a>
    <p style="margin-top:16px;font-size:11px;">mydua.ai — support@mydua.ai</p>
  </div>
</div></body></html>""")


@app.get("/api/gift/status/{gift_id}")
async def gift_status(gift_id: str):
    """Check delivery status of a gift."""
    gift = db.gift_get(gift_id)
    if not gift:
        raise HTTPException(404, "Gift not found.")
    return {
        "giftId": gift["gift_id"],
        "status": gift["delivery_status"],
        "method": gift["delivery_method"],
        "recipientName": gift["recipient_name"],
    }


@app.get("/api/gift/config")
async def gift_config():
    """Return gift feature availability and pricing."""
    return {
        "email": {"available": bool(SMTP_USERNAME), "price": 0},
        "sms": {"available": bool(TWILIO_ACCOUNT_SID), "price": GIFT_SMS_PRICE / 100},
        "postcard": {"available": bool(LOB_API_KEY), "price": GIFT_POSTCARD_PRICE / 100},
    }


# ══════════════════════════════════════════════════
# Stripe — "Support Us" Donations
# ══════════════════════════════════════════════════

SUPPORT_AMOUNTS = {
    "5": {"label": "$5 — A Small Kindness", "cents": 500},
    "10": {"label": "$10 — May Allah Reward You", "cents": 1000},
    "25": {"label": "$25 — Generous Support", "cents": 2500},
    "50": {"label": "$50 — Sadaqah Jariyah", "cents": 5000},
}


@app.get("/api/stripe-config")
async def stripe_config():
    if not STRIPE_PUBLISHABLE_KEY:
        raise HTTPException(500, "Stripe is not configured.")
    return {"publishableKey": STRIPE_PUBLISHABLE_KEY}


@app.post("/api/create-support-session")
async def create_support_session(req: SupportRequest):
    if not STRIPE_SECRET_KEY:
        raise HTTPException(500, "Stripe is not configured.")

    if req.amount == "custom":
        if req.customAmount < 1:
            raise HTTPException(400, "Please enter an amount of at least $1.")
        amount_cents = req.customAmount * 100
        label = f"${req.customAmount} — Custom Support"
    elif req.amount in SUPPORT_AMOUNTS:
        amount_cents = SUPPORT_AMOUNTS[req.amount]["cents"]
        label = SUPPORT_AMOUNTS[req.amount]["label"]
    else:
        raise HTTPException(400, "Invalid amount.")

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": "Support mydua.ai", "description": label},
                    "unit_amount": amount_cents,
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=f"{APP_BASE_URL}/support-thank-you",
            cancel_url=f"{APP_BASE_URL}/#support",
            submit_type="donate",
        )
        db.track("donations_initiated")
        return {"url": session.url}
    except stripe.error.StripeError as e:
        logger.error(f"Stripe error: {e}")
        raise HTTPException(500, f"Payment error: {str(e)}")


@app.get("/support-thank-you", response_class=HTMLResponse)
async def support_thank_you():
    share_text = "I just supported mydua.ai — a free tool that writes personalized du'as for your family for any occasion. Try it: " + APP_BASE_URL + "/?ref=share"
    wa_url = f"https://wa.me/?text={share_text.replace(' ', '%20')}"
    tw_url = f"https://twitter.com/intent/tweet?text={share_text.replace(' ', '%20')}"

    return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>Jazak'Allah Khair — mydua.ai</title>
  <link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@300;400;600;700&family=Amiri:wght@400;700&display=swap" rel="stylesheet"/>
  <style>
    *{{margin:0;padding:0;box-sizing:border-box;}}
    body{{font-family:'Cormorant Garamond',serif;background:linear-gradient(160deg,#0a1628 0%,#112240 40%,#0d1f3c 70%,#091525 100%);color:#e8dcc8;min-height:100vh;display:flex;align-items:center;justify-content:center;text-align:center;padding:40px 20px;}}
    .card{{max-width:540px;background:rgba(255,255,255,0.03);border:1px solid rgba(201,169,110,0.15);border-radius:20px;padding:48px 40px;backdrop-filter:blur(10px);}}
    .bismillah{{font-family:'Amiri',serif;font-size:28px;color:#c9a96e;direction:rtl;opacity:0.8;margin-bottom:16px;}}
    h1{{font-size:32px;font-weight:300;color:#e8dcc8;margin-bottom:12px;}}
    .gold{{color:#c9a96e;font-weight:600;}}
    p{{font-size:17px;color:#d4c9b4;line-height:1.7;margin-bottom:16px;}}
    .arabic{{font-family:'Amiri',serif;font-size:22px;color:#c9a96e;margin:20px 0;}}
    .share-section{{margin-top:28px;padding-top:24px;border-top:1px solid rgba(201,169,110,0.15);}}
    .share-title{{font-size:18px;color:#c9a96e;font-weight:600;margin-bottom:8px;}}
    .share-subtitle{{font-size:14px;color:#8a7d6b;font-style:italic;margin-bottom:16px;}}
    .share-btns{{display:flex;gap:10px;justify-content:center;flex-wrap:wrap;}}
    .share-btn{{padding:12px 24px;border-radius:10px;text-decoration:none;font-size:15px;font-weight:600;font-family:'Cormorant Garamond',serif;transition:all 0.3s;cursor:pointer;border:none;}}
    .share-wa{{background:#25D366;color:#fff;}}.share-wa:hover{{background:#1da851;}}
    .share-tw{{background:#1DA1F2;color:#fff;}}.share-tw:hover{{background:#0d8bd9;}}
    .share-copy{{background:rgba(201,169,110,0.12);color:#c9a96e;border:1px solid rgba(201,169,110,0.3);}}.share-copy:hover{{background:rgba(201,169,110,0.2);}}
    .back-link{{display:inline-block;margin-top:24px;padding:12px 32px;border:1px solid rgba(201,169,110,0.3);border-radius:10px;color:#c9a96e;text-decoration:none;font-size:16px;font-weight:600;transition:all 0.3s;}}
    .back-link:hover{{background:rgba(201,169,110,0.08);}}
  </style>
</head>
<body>
  <div class="card">
    <div class="bismillah">بِسْمِ اللَّهِ الرَّحْمَنِ الرَّحِيمِ</div>
    <h1><span class="gold">Jazak'Allah Khair</span></h1>
    <p>May Allah reward you abundantly for your generosity. Your support helps us keep this service free for Muslims around the world.</p>
    <div class="arabic">جَزَاكَ اللَّهُ خَيْرًا</div>
    <p>May your donation be a <strong style="color:#c9a96e;">sadaqah jariyah</strong> — a continuous charity that benefits you in this life and the next.</p>

    <div class="share-section">
      <div class="share-title">Multiply your reward</div>
      <div class="share-subtitle">Share mydua.ai with others — every du'a they make is sadaqah jariyah for you too</div>
      <div class="share-btns">
        <a href="{wa_url}" target="_blank" class="share-btn share-wa">Share on WhatsApp</a>
        <a href="{tw_url}" target="_blank" class="share-btn share-tw">Share on X</a>
        <button class="share-btn share-copy" onclick="navigator.clipboard.writeText('{APP_BASE_URL}/?ref=share');this.textContent='Copied!';setTimeout(()=>this.textContent='Copy Link',2000);">Copy Link</button>
      </div>
    </div>

    <a href="/" class="back-link">Generate a Du'a for Your Family</a>
  </div>
</body>
</html>""")


# ══════════════════════════════════════════════════
# Static Files (must be last)
# ══════════════════════════════════════════════════

app.mount("/", StaticFiles(directory=str(BASE_DIR / "static"), html=True), name="static")
