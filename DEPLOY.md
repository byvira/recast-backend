# Deploy — Render

This captures the deploy configuration that otherwise lives only in the Render
dashboard, so it's reviewable and restorable from the repo. Values here are
**names**, not secrets — actual values live in Render's Environment tab and in
your local `.env` (never committed).

## Runtime

- Python: `3.11.9` (pinned in `runtime.txt`)
- Framework: FastAPI 0.115 / Uvicorn

## Start command

```
uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*"
```

`--proxy-headers --forwarded-allow-ips="*"` is required behind Render's reverse
proxy — without it, `slowapi`'s `get_remote_address()` sees Render's proxy IP
for every request, and per-IP rate limits (signup, OTP, login) collapse into a
single global bucket.

## Health check

- Path: `/health`
- Point Render's own health check here — it's a shallow `{"status":"ok"}` with
  no DB/Redis touch, so a slow dependency doesn't kill the instance.
- `/health/ready` (once added — see hardening backlog) is for your uptime
  monitor, not Render's health check: it actually pings Mongo + Redis.

## Plan / instance count

> _Fill in current Render plan and instance count here — e.g. "Starter, 1
> instance" — once confirmed against the dashboard._

**Known constraint:** the in-process `AsyncIOScheduler` (`process_scheduled_posts`
every 1 min, `refresh_expiring_tokens` every 24h, `refresh_analytics` every 6h)
is not safe across >1 instance — each job would double-fire. Do not scale past
1 web instance until that's addressed (locking or moved to Render Cron).

## Network access

- MongoDB Atlas → Network Access must allow Render's egress IPs (or `0.0.0.0/0`
  if the plan can't pin them).
- Upstash Redis is reachable via public host + token, no allowlist needed.

## Environment variables

Set every variable below in Render → Environment. Everything not marked
**required** defaults to `""`/`False` in `config.py` and fails quietly at call
time rather than at boot — only the three **required** ones crash on missing.

### Core (required)

| Variable | Notes |
|---|---|
| `SECRET_KEY` | **Required.** Must be a real random value, distinct from the local `.env` value. Generate with `python -c "import secrets; print(secrets.token_hex(32))"`. |
| `MONGODB_URL` | **Required.** |
| `REDIS_URL` | **Required.** |
| `ENVIRONMENT` | Set to `production`. Flips OTP delivery to Resend/Twilio, restricts CORS to `PRODUCTION_DOMAIN`, drops log verbosity. |

### Pending pre-launch blockers — do not deploy to production until resolved

| Variable | Status | Action |
|---|---|---|
| `PRODUCTION_DOMAIN` | **BLOCKED** — no frontend deployed yet | Must be the frontend's origin (e.g. `https://app.yourdomain.com`), not the backend's own URL. CORS breaks for real browser calls until this is set correctly. Revisit once the frontend ships. |
| `EMAIL_FROM` | **BLOCKED** — still a gmail.com address | Resend rejects sends from free-mail domains. Requires: (1) own a domain, (2) verify it in Resend, (3) set `EMAIL_FROM=noreply@yourdomain.com`. Until then, production OTP email will fail. |

### Auth / JWT

`ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `JWT_EXPIRE_HOURS`, `JWT_REFRESH_EXPIRE_DAYS`,
`JWT_ISSUER`, `JWT_AUDIENCE`, `OTP_EXPIRE_MINUTES`, `OTP_MAX_ATTEMPTS`,
`OTP_MAX_SENDS_PER_HOUR`, `OTP_MAX_SENDS_PER_DAY`, `OTP_COOLDOWN_SECONDS`,
`OTP_LOCK_MINUTES`

### Email / SMS

`RESEND_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`

### LLM

`LLM_PROVIDER` (currently `groq`), `GROQ_API_KEY`, `GROQ_TPM_LIMIT` (bump after
upgrading Groq's plan — see `app/shared/llm.py`), `GEMINI_API_KEY` (fallback
provider)

### Observability (LangSmith tracing — already wired)

`LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY`, `LANGCHAIN_PROJECT`, `LANGCHAIN_ENDPOINT`

### Data stores / storage

`CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`

### Token encryption

`FERNET_SECRET_KEY` — encrypts OAuth tokens in `workspace_connections`.
Rotating this without a re-encrypt migration makes every stored token
undecryptable — see the ops runbook (pending, Batch 6) before rotating.

### OAuth — one block per provider, redirect URI must match the provider
console byte-for-byte (https, no trailing slash)

- **Meta** (Instagram + Threads + Facebook): `META_APP_ID`, `META_APP_SECRET`, `META_REDIRECT_URI`, `META_CONFIG_ID`
- **Threads**: `THREADS_APP_ID`, `THREADS_APP_SECRET`, `THREADS_REDIRECT_URI`
- **LinkedIn**: `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`, `LINKEDIN_REDIRECT_URI`
- **Google**: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`
- **Twitter/X**: `TWITTER_API_KEY`, `TWITTER_API_SECRET`, `TWITTER_BEARER_TOKEN`, `TWITTER_REDIRECT_URI`
- **Reddit**: `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_REDIRECT_URI`, `REDDIT_USER_AGENT`
- **Bluesky**: `BLUESKY_APP_NAME`, `BLUESKY_SERVICE_URL`, `BLUESKY_TEST_APP_PASSWORD` (test only)

### Alerts

`SLACK_WEBHOOK_URL`, `ALERT_EMAIL` — both currently blank; `alerts.py` no-ops
until set.

### Misc

`APP_NAME`, `FREE_CREDITS_LIMIT`, `PUBLISH_MAX_RETRIES`, `PUBLISH_RETRY_BASE_DELAY`,
`TOKEN_REFRESH_THRESHOLD_MINUTES`

### Not wired to any code path — do not carry forward to Render

`HUGGINGFACE_API_KEY`, `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET`,
`STRIPE_SECRET_KEY`, `DEBUG`, `ALLOWED_ORIGINS` (see hardening backlog — wire
in or delete), `JWT_ALGORITHM` (redundant with `ALGORITHM`, being removed)
