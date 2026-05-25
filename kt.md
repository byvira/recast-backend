# Auth & Brand Voice Setup — Knowledge Transfer Document
Version: 1.0
Last Updated: May 2026

---

## 1. Overview

This document covers the complete auth flow and brand voice
onboarding system for the SaaS backend.

---

## 2. Auth System

### 2.1 Philosophy
- Passwordless only — OTP via email or SMS
- JWT-based sessions (access + refresh token pair)
- All OTP state lives in Redis only — never MongoDB
- Security layered at: Redis, slowapi, middleware

### 2.2 OTP Flow

REQUEST OTP
  POST /api/v1/auth/request-otp
  Body: { identifier, channel }

  Redis checks (in order):
  1. is_locked?          → 423 Locked
  2. cooldown active?    → 429 (wait 60s)
  3. hour_count >= 5?    → 429 (wait until window resets)
  4. day_count >= 10?    → 429 (wait until tomorrow)

  On pass:
  → generate 6-digit OTP
  → store otp:{identifier}:code in Redis (TTL 10min)
  → increment hour_count (TTL 1hr)
  → increment day_count  (TTL 24hr)
  → set cooldown         (TTL 60s)
  → send via Resend (email) or Twilio (SMS)

VERIFY OTP
  POST /api/v1/auth/verify-otp
  Body: { identifier, otp, channel }

  Checks:
  1. is_locked?           → 423
  2. code exists?         → 401 expired
  3. code matches?        → increment attempts on fail
  4. attempts >= 5?       → set locked (TTL 15min) → 423

  On success:
  → delete otp code from Redis
  → set otp:{identifier}:verified = true (TTL 10min)
  → return { valid: true, is_new_user: bool }

SIGNUP (new user)
  POST /api/v1/auth/signup
  Body: { identifier, otp, channel, name, username }

  Checks:
  1. verified flag exists in Redis?  → 401 if not
  2. username valid format?          → 422 if not
  3. username taken?                 → 409 Conflict

  On pass:
  → create UserProfile in MongoDB
  → clear OTP state from Redis
  → issue access_token (24hr) + refresh_token (30 days)
  → return tokens + UserProfileResponse

LOGIN (existing user)
  POST /api/v1/auth/login
  Body: { identifier, otp, channel }

  Checks:
  1. verified flag exists?  → 401 if not
  2. user exists?           → 404 (redirect to signup)

  On pass:
  → update last_active in MongoDB
  → clear OTP state from Redis
  → issue access_token + refresh_token
  → return tokens + UserProfileResponse

### 2.3 Token Strategy

  Access Token:
  - JWT signed with SECRET_KEY
  - Expires: 24 hours
  - Payload: { user_id, exp, iat }
  - Sent in: Authorization: Bearer <token>

  Refresh Token:
  - JWT signed with SECRET_KEY
  - Expires: 30 days
  - Use: POST /api/v1/auth/refresh
  - Returns: new access_token only

  Logout:
  - Blacklist token in Redis: blacklist:{token} (TTL = remaining expiry)
  - get_current_user checks blacklist on every request

### 2.4 Rate Limiting Layers

  Layer 1 — slowapi (global, per IP):
    Default routes:   100 req/min
    Auth routes:       20 req/min
    OTP routes:         5 req/min

  Layer 2 — Redis OTP limits (per identifier):
    Cooldown:          60s between sends
    Hourly limit:       5 sends/hour
    Daily limit:       10 sends/day
    Attempt lock:       5 wrong attempts → 15min lock

  Headers returned on every response:
    X-RateLimit-Limit
    X-RateLimit-Remaining
    X-RateLimit-Reset

### 2.5 Redis Key Schema

  otp:{identifier}:code        TTL: 10min  → OTP value
  otp:{identifier}:attempts    TTL: 15min  → wrong attempt count
  otp:{identifier}:locked      TTL: 15min  → lock flag
  otp:{identifier}:cooldown    TTL: 60s    → resend cooldown
  otp:{identifier}:hour_count  TTL: 1hr    → hourly send count
  otp:{identifier}:day_count   TTL: 24hr   → daily send count
  otp:{identifier}:verified    TTL: 10min  → post-verify flag
  blacklist:{token}            TTL: remaining JWT expiry

---

## 3. User Profile

### 3.1 Signup Screen Fields (shown to user)
  name        required
  username    required, auto-generated from name, editable

### 3.2 Auto-filled on Creation (not shown)
  id                → UUID
  email/phone       → from OTP
  timezone          → detected from browser
  language          → detected from browser
  plan              → "free"
  credits_used      → 0
  credits_limit     → 100
  onboarding_done   → false
  brand_profiles    → []

### 3.3 Filled Later (profile settings)
  bio, website, avatar_url, preferred_platforms

### 3.4 Username Rules
  - 3–30 characters
  - Alphanumeric + underscore only
  - Auto-generated: "John Doe" → "johndoe"
  - If taken → "johndoe_4821" (random 4 digits)
  - Check: GET /api/v1/auth/check-username/{username}

---

## 4. Brand Voice Setup

### 4.1 Overview
  Each user can have up to 10 brand profiles.
  Brand profiles are created after signup.
  Onboarding is step-by-step, progress saved per step.

### 4.2 Brand Types and Step Counts
  Person:          5 steps
  Personal Brand:  6 steps
  Business:        6 steps
  Product:         6 steps

### 4.3 Step Flow

  ALL TYPES — Step 1:
    Select brand type (Person/Personal Brand/Business/Product)
    POST /api/v1/brand/
    Body: { brand_type }
    Returns: { brand_profile_id }

  PERSON — Steps 2-5:
    Step 2: Identity  → name, headline, bio, location, goals
    Step 3: Audience  → who, reading_level, knowledge_base, pain_point
    Step 4: Voice     → tones, humor, emoji, writing_style
    Step 5: Platforms → platform selection

  PERSONAL BRAND — Steps 2-6:
    Step 2: Identity  → name, niche, tagline, core_message
    Step 3: Pillars   → content_pillars, monetization
    Step 4: Audience  → avatar, transformation, pain_point
    Step 5: Voice     → tones, humor, emoji, cta_style
    Step 6: Platforms → platform selection

  BUSINESS — Steps 2-6:
    Step 2: Identity  → name, industry, tagline, mission
    Step 3: ICP       → ideal_customer, job_titles, buying_trigger
    Step 4: Audience  → reading_level, knowledge_base, pain_point
    Step 5: Voice     → tones, formality, approach_style
    Step 6: Platforms → platform selection

  PRODUCT — Steps 2-6:
    Step 2: Identity    → name, category, one_liner, problem_solved
    Step 3: Positioning → key_features, pricing, alternatives_used
    Step 4: Audience    → primary_user, switching_trigger, pain_point
    Step 5: Voice       → tones, messaging_style, social_proof_style
    Step 6: Platforms   → platform selection

### 4.4 Saving Step Progress
  PUT /api/v1/brand/{brand_id}/step
  Body: { step: 2, data: { ...step fields } }
  → Merges data into brand profile document
  → Safe to call multiple times (idempotent)
  → Returns next_step

### 4.5 Completing Onboarding
  PUT /api/v1/brand/{brand_id}/complete
  → Sets is_complete = true on brand profile
  → Sets onboarding_done = true on UserProfile
    (only if this is the first completed brand profile)

### 4.6 Setup Path (Step 5 for all types)
  extract:
    Upload files → system extracts voice samples
    Connect social accounts → scrape existing content
    System generates confidenceScore (0-100)
    Extracted samples stored in extraction_data

  manual:
    User provides:
    - Openers (how they start posts)
    - Closers (how they end posts)
    - Signature phrases (with placement tags)
    - Banned words list
    - Preferred synonyms

### 4.7 Brand Profile in Agents
  Every agent node receives brand profile via initial_state():
    initial_state(user_id, brand_profile_dict, raw_input)

  build_brand_context() in utils/brand.py converts
  brand profile dict → system prompt string injected
  into every LLM call.

---

## 5. API Route Summary

  Auth:
  POST   /api/v1/auth/request-otp
  POST   /api/v1/auth/verify-otp
  POST   /api/v1/auth/signup
  POST   /api/v1/auth/login
  POST   /api/v1/auth/refresh
  POST   /api/v1/auth/logout          🔒
  GET    /api/v1/auth/check-username/{username}

  Users:
  GET    /api/v1/users/me             🔒
  PUT    /api/v1/users/me             🔒
  GET    /api/v1/users/{username}

  Brand:
  POST   /api/v1/brand/               🔒
  GET    /api/v1/brand/               🔒
  GET    /api/v1/brand/{brand_id}     🔒
  PUT    /api/v1/brand/{brand_id}/step     🔒
  PUT    /api/v1/brand/{brand_id}/complete 🔒
  DELETE /api/v1/brand/{brand_id}     🔒

  🔒 = requires Authorization: Bearer <access_token>

---

## 6. Error Code Reference

  400  Bad Request       → invalid input format
  401  Unauthorized      → invalid/expired token or OTP
  403  Forbidden         → valid token, wrong user
  404  Not Found         → user/brand not found
  409  Conflict          → username already taken
  422  Validation Error  → Pydantic validation failed
  423  Locked            → too many OTP attempts
  429  Too Many Requests → rate limit hit

---

## 7. Environment Variables Required

  # Auth
  SECRET_KEY
  JWT_EXPIRE_HOURS
  JWT_REFRESH_EXPIRE_DAYS

  # OTP
  OTP_EXPIRE_MINUTES
  OTP_MAX_ATTEMPTS
  OTP_MAX_SENDS_PER_HOUR
  OTP_MAX_SENDS_PER_DAY
  OTP_COOLDOWN_SECONDS

  # Email
  RESEND_API_KEY
  EMAIL_FROM

  # SMS
  TWILIO_ACCOUNT_SID
  TWILIO_AUTH_TOKEN
  TWILIO_PHONE_NUMBER

  # DB
  MONGODB_URL
  REDIS_URL