"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central settings object populated from .env or environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )

    APP_NAME: str = "Recast-Backend"

    # Environment — controls CORS origins and notification delivery
    # Set to "production" to restrict CORS and enable real email/SMS delivery
    ENVIRONMENT: str = "development"

    BLUESKY_TEST_APP_PASSWORD: str = ""

    # Alerts
    SLACK_WEBHOOK_URL: str = ""
    ALERT_EMAIL: str = ""

    # Google OAuth
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "https://recast-api.byvirastudio.com/api/v1/oauth/google/callback"

    # Security
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # Auth — OTP rate limits and JWT configuration
    OTP_EXPIRE_MINUTES: int = 10
    OTP_MAX_ATTEMPTS: int = 5
    OTP_MAX_SENDS_PER_HOUR: int = 10
    OTP_MAX_SENDS_PER_DAY: int = 30
    OTP_COOLDOWN_SECONDS: int = 30
    OTP_LOCK_MINUTES: int = 15

    # Dev/test-only OTP backdoor for automated (Playwright) testing — see
    # docs/PLAYWRIGHT_TESTING_GUIDE.md. Comma-separated allowlist of
    # identifiers GET /api/v1/auth/dev/last-otp will serve. Only ever
    # consulted when ENVIRONMENT != "production" (enforced in auth.py,
    # not here) — this setting alone does not grant access in production.
    DEV_OTP_TEST_IDENTIFIERS: str = "virastudio.hello@gmail.com"
    # Sign-in cookies in production. "none" lets the site and the API sit on unrelated addresses (the older setup). When both are on the same
    # registrable domain (recast.example.com and api.example.com) "lax" is safer, because the browser then never sends the cookie from other
    # websites. COOKIE_DOMAIN is left empty so each cookie belongs to the API host alone.
    COOKIE_SAMESITE: str = "none"
    COOKIE_DOMAIN: str = ""
    # How long an access token lives. Short on purpose: the refresh token (rotated on every use) keeps a person signed in, so a copied
    # access token stops working within the hour.
    JWT_EXPIRE_HOURS: int = 1
    JWT_REFRESH_EXPIRE_DAYS: int = 30
    JWT_ISSUER: str = "saas-backend"
    JWT_AUDIENCE: str = "saas-api"

    # Email — Resend API
    RESEND_API_KEY: str = ""
    # The address emails are sent from, on a domain verified in Resend (for example hello@mail.example.com, or `Name <hello@mail.example.com>`).
    # Left empty, Resend's test sender is used, which only delivers to the Resend account's owner.
    EMAIL_FROM: str = ""

    # SMS — Twilio
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_PHONE_NUMBER: str = ""

    # ElevenLabs — present in .env but not yet wired to any code (no caller
    # anywhere in the app). Declared here only so pydantic-settings' strict
    # mode (extra="forbid") doesn't fail Settings() construction on an
    # otherwise-valid .env — this was blocking the entire test suite from
    # even collecting. A real audio/TTS pipeline is the actual place this
    # gets used, not built yet (see the plan's video/audio pipeline note).
    ELEVEN_LABS: str = ""

    # Set once Google has passed Recast's YouTube API compliance audit. Until then Google keeps every video uploaded through the API
    # private, so Recast offers Private only (see youtube/metadata.visibility_problem).
    YOUTUBE_API_AUDIT_PASSED: bool = False

    # LLM provider — "groq" | "gemini"
    LLM_PROVIDER: str = "groq"
    GROQ_API_KEY: str = ""
    GEMINI_API_KEY: str = ""
    # Groq's tokens-per-minute ceiling for this account/tier — prompt tokens
    # PLUS the max_tokens requested both count against this, so it must stay
    # correct for whatever plan is active, not just the free tier's current
    # 8000. Bump this one value after upgrading Groq's plan — no code change
    # needed. See app.shared.llm._safe_max_tokens, which uses it to size
    # every request so prompt + requested output never exceeds it.
    GROQ_TPM_LIMIT: int = 8000

    # ── LangSmith tracing (agent observability) ──────────────────────────
    # Set LANGCHAIN_TRACING_V2=true and LANGCHAIN_API_KEY=ls__... in .env to make
    # every personal/supervisor graph run inspectable in LangSmith. When unset,
    # app.core.tracing.ainvoke_traced is a transparent pass-through.
    LANGCHAIN_TRACING_V2: bool = False
    LANGCHAIN_API_KEY: str = ""
    LANGCHAIN_PROJECT: str = "recast-agents"
    LANGCHAIN_ENDPOINT: str = "https://api.smith.langchain.com"

    # Data stores
    MONGODB_URL: str
    REDIS_URL: str

    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""

    # Cloudflare Workers AI — image generation provider (Row 11,
    # app.pipelines.media.image_generation). Free tier: 10,000 neurons/day,
    # hard block on exhaustion, no surprise billing. At the 1024x1024/4-step
    # settings this app actually uses, one image costs 57.6 neurons (4 steps
    # x 9.6 neurons + four 512x512 tiles x 4.8 neurons — verified against
    # Cloudflare's own pricing), so the real daily ceiling is 10,000/57.6 =
    # ~173 images/day for the WHOLE app (one shared account, not per
    # workspace) — corrected 2026-09-25 from an earlier "~200-500" estimate
    # that didn't account for the real per-image neuron cost at this size.
    CLOUDFLARE_API_TOKEN: str = ""
    CLOUDFLARE_ACCOUNT_ID: str = ""
    # Denoising steps for Cloudflare FLUX schnell (1 to 8). 4 costs about 57.6 neurons a picture (about 173 a day on the
    # free allowance); 8 looks cleaner, costs about 96 (about 104 a day). Leave at 4 on the free plan.
    CLOUDFLARE_IMAGE_STEPS: int = 4
    # A second model reviews each finished long-form draft and sends weak ones back for one targeted rewrite. It costs one
    # small model call per post; turn it off to save quota.
    TEXT_CRITIQUE_ENABLED: bool = True

    # Open-model fallbacks for when Gemini and Cloudflare are not answering (app/shared/open_fallbacks.py).
    # Each one is skipped when its key is empty. Model names are settings because free model lists change often.
    MISTRAL_API_KEY: str = ""
    MISTRAL_MODEL: str = "open-mistral-nemo"  # checked live 2026-10-01; mistral-small-latest was rate limited on the free plan
    OPENROUTER_API_KEY: str = ""
    # NVIDIA's free hosted models (OpenAI compatible, email signup, no card). Third writing backup; skipped when empty.
    NVIDIA_API_KEY: str = ""
    NVIDIA_MODEL: str = "meta/llama-3.3-70b-instruct"
    # Picture understanding when Gemini is not answering: free OpenRouter models that accept images (checked 2026-10-01).
    OPENROUTER_VISION_MODEL: str = "qwen/qwen3.8-27b:free"
    OPENROUTER_VISION_MODEL_2: str = "google/gemma-4-31b-it:free"
    OPENROUTER_VISION_MODEL_3: str = "openrouter/free"  # OpenRouter picks whichever free model is available; free models are often busy
    OPENROUTER_MODEL_2: str = "openrouter/free"  # tried when the first free model is busy; OpenRouter picks any free model that is up
    OPENROUTER_MODEL: str = "qwen/qwen3.8-27b:free"  # checked live 2026-10-01; openai/gpt-oss-120b:free had been retired
    HUGGINGFACE_API_TOKEN: str = ""
    HUGGINGFACE_IMAGE_MODEL: str = "black-forest-labs/FLUX.1-schnell"
    # The old hf-inference route is gone (410). Pictures go through a provider Hugging Face routes to; nscale was checked live.
    HUGGINGFACE_IMAGE_PROVIDER: str = "nscale"
    # Pollinations needs no key. Turn it off here if you would rather show "no picture" than a free-service picture.
    POLLINATIONS_ENABLED: bool = True
    # Read each connected account's picture, display name and follower count from its platform (in the background, at most once a day).
    PROFILE_REFRESH_ENABLED: bool = True

    # Cloudflare Turnstile — spam protection on the public share page's
    # guest comment form (an anonymous form with no account behind it).
    # Empty means Turnstile isn't checked yet: the honeypot field and the
    # real rate limit still apply on their own, so the form works before
    # a real site/secret key pair is set up, just without this extra layer.
    TURNSTILE_SECRET_KEY: str = ""

    # ElevenLabs TTS — real, paid plan, product owner's own account, decided
    # 2026-09-25 specifically for the audio pipeline (see
    # pow/audio_image_pipeline/02-audio-pipeline-plan.md's TTS integration
    # section). Deliberately on its own account, not Cloudflare's Workers AI
    # TTS models — those share the same 10,000-neuron/day pool as Flux image
    # generation, and one narration could burn most of that shared budget.
    # Empty until the user provides it — see pow/.../PROGRESS.md's Blockers.
    # Confirmed live 2026-09-26 the account is still on its free plan
    # (library voices 402 "payment_required" via the API) — a real Azure
    # TTS fallback was built and live-tested the same day, then removed
    # the same day per the user's explicit choice: keep ElevenLabs only,
    # upgrade the account later. Not re-added without a new decision —
    # see pow/audio_image_pipeline/PROGRESS.md's Decisions Log.
    ELEVENLABS_API_KEY: str = ""
    # Set false to skip ElevenLabs entirely (narration goes straight to Deepgram), for example while the account is
    # on a plan that cannot use the voices the product needs. Switch back on after upgrading.
    ELEVENLABS_ENABLED: bool = True

    # Deepgram — added 2026-09-26 by the user directly to .env (not yet
    # wired into any code path) to check real feasibility as a TTS option
    # while ElevenLabs' account is still on its free plan. Aura (Deepgram's
    # TTS product) was in file 03's original research: real accounts get a
    # $200 one-time free credit, no expiration — a one-time signup credit,
    # not a recurring free tier, standard billing applies once spent. Not
    # yet a decided pick — this is a live feasibility check only.
    DEEPGRAM_API_KEY: str = ""

    # Jamendo — free, keyless-to-browse CC-licensed music catalog, used once
    # (app.pipelines.media.music_library, scripts/seed_music_library.py) to
    # seed a shared, CC0-only curated pack for the Music tab's bed picker.
    # Get a free client_id at https://devportal.jamendo.com/ (no card).
    # Empty until the user provides it — the seed script refuses to run
    # without it rather than guessing/using an undisclosed shared key.
    JAMENDO_CLIENT_ID: str = ""

    # Gemini Nano Banana (gemini-2.5-flash-image) as a PAID fallback for
    # image generation, only used once Cloudflare's free ~173/day is
    # exhausted. $0.039/image — capped so an exhausted free tier can't turn
    # into unbounded spend. 0 disables the fallback entirely (Cloudflare
    # exhaustion then falls straight through to the quote-card template,
    # the pre-2026-09-25 behavior). A settings value, not hardcoded, so the
    # cap is a config change, not a code change.
    GEMINI_IMAGE_FALLBACK_DAILY_CAP: int = 100

    # Free tier credits limit
    FREE_CREDITS_LIMIT: int = 100

    # CORS — JSON array string in .env, e.g. ALLOWED_ORIGINS=["https://a.com","https://b.com"]
    # In development this is the full origin list. In production it's merged
    # with PRODUCTION_DOMAIN, so use it for any *additional* prod origins
    # (staging frontend, a second domain) beyond the primary one.
    ALLOWED_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # Production domain — required when ENVIRONMENT=production
    # Example: "https://app.yourdomain.com"
    PRODUCTION_DOMAIN: str = ""

    # Frontend base URL — used to build links embedded in emails (invite
    # accept, OAuth reconnect, dashboard). Deliberately separate from
    # PRODUCTION_DOMAIN, which feeds CORS and in this deployment points at
    # the backend's own Render URL, not the frontend.
    FRONTEND_URL: str = "https://recast.byvirastudio.com"

    # ── API docs gating ────────────────────────────────────────────────────
    # /docs, /redoc, /scalar and /openapi.json are open in development. In
    # production they require HTTP Basic auth using these credentials — if
    # either is unset, the docs routes are unreachable (never open by default).
    DOCS_USERNAME: str = ""
    DOCS_PASSWORD: str = ""

    # ── Logging & error tracking ────────────────────────────────────────────
    # LOG_FORMAT: "console" (colorlog, human-readable) | "json" (structlog,
    # one JSON object per line — what a log aggregator needs). Defaults to
    # json in production, console in development; set explicitly to override.
    LOG_FORMAT: str = ""
    # Sentry DSN for error tracking. Blank = Sentry is never initialised.
    SENTRY_DSN: str = ""

    # ── Token Encryption ──────────────────────────────────────────────────
    # Used to encrypt/decrypt OAuth access tokens and refresh tokens in MongoDB.
    # Generate with:
    # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    FERNET_SECRET_KEY: str = ""

    # ── Meta — Instagram + Threads + Facebook ─────────────────────────────
    # One Meta app covers all three platforms.
    # Register at: developers.facebook.com/apps
    # Local dev: use ngrok URL as redirect URI (Meta blocks plain localhost)
    # Production: replace with https://yourdomain.com/api/v1/oauth/meta/callback
    META_APP_ID: str = ""
    META_APP_SECRET: str = ""
    META_REDIRECT_URI: str = "https://recast-api.byvirastudio.com/api/v1/oauth/meta/callback"

    # ── LinkedIn ──────────────────────────────────────────────────────────
    # Register at: developer.linkedin.com/apps
    # Scopes needed: w_member_social, r_basicprofile
    LINKEDIN_CLIENT_ID: str = ""
    LINKEDIN_CLIENT_SECRET: str = ""
    LINKEDIN_REDIRECT_URI: str = "https://recast-api.byvirastudio.com/api/v1/oauth/linkedin/callback"

    # ── Twitter / X ───────────────────────────────────────────────────────
    # Register at: developer.twitter.com/portal
    # Requires $100/mo Basic tier for write access (posting)
    # Scopes needed: tweet.read, tweet.write, users.read
    TWITTER_API_KEY: str = ""
    TWITTER_API_SECRET: str = ""
    TWITTER_BEARER_TOKEN: str = ""
    TWITTER_REDIRECT_URI: str = "https://recast-api.byvirastudio.com/api/v1/oauth/twitter/callback"

    # ── Reddit ────────────────────────────────────────────────────────────
    # Register at: reddit.com/prefs/apps → create web app
    # Scopes needed: submit, identity, read
    REDDIT_CLIENT_ID: str = ""
    REDDIT_CLIENT_SECRET: str = ""
    REDDIT_REDIRECT_URI: str = "https://recast-api.byvirastudio.com/api/v1/oauth/reddit/callback"
    # User-Agent format required by Reddit API — update version as needed
    REDDIT_USER_AGENT: str = "ViraStudio/1.0"
    BLUESKY_APP_NAME: str = "recast"
    META_CONFIG_ID: str = ""

    THREADS_APP_ID: str = ""
    THREADS_APP_SECRET: str = ""
    THREADS_REDIRECT_URI: str = "https://recast-api.byvirastudio.com/api/v1/oauth/threads/callback"

    # ── Bluesky ───────────────────────────────────────────────────────────
    # No developer registration needed — uses AT Protocol auth.
    # Users connect via their handle + an App Password (not their main password).
    # App Passwords: bsky.app → Settings → Privacy and Security → App Passwords
    BLUESKY_SERVICE_URL: str = "https://bsky.social"

    # ── Publish Pipeline ──────────────────────────────────────────────────
    # Max retries before supervisor flags a post for human review
    PUBLISH_MAX_RETRIES: int = 3
    # Longest script one narration is made from. Our own guard against a runaway request, not a provider limit.
    AUDIO_SCRIPT_MAX_CHARS: int = 15000
    # On (owner decision 2026-10-03): a post in one non-English language under the brand's own tone keeps English brand, product,
    # technical and business terms in English, translates everyday words, and keeps any word native speakers normally say in
    # English. Switch off with ENGLISH_TERMS_STAY_ENGLISH=false.
    ENGLISH_TERMS_STAY_ENGLISH: bool = True
    # Off by the owner's decision until about a month of real posts exists (turn on around 2026-11-03): when on, the writing prompt
    # gets one sentence naming how the workspace's best measured post on that platform opened (needs at least 3 measured posts).
    PERFORMANCE_HINT_IN_PROMPTS: bool = False
    # Seconds to wait between retry attempts (base — multiplied per attempt)
    PUBLISH_RETRY_BASE_DELAY: int = 5
    # How many minutes ahead to refresh tokens before they expire
    TOKEN_REFRESH_THRESHOLD_MINUTES: int = 10080  # 7 days
    # Server-side gate on Publish Now, Schedule and the scheduled worker:
    # only approved pieces go out, and a flagged piece needs an explicit
    # "publish anyway". Emergency switch only — set False to lift every check.
    PUBLISH_REQUIRE_APPROVAL: bool = True

    # Comma separated workspace ids that count as the Ops workspace for the platforms module: a platform whose
    # rollout is "Ops workspace only" is available only in these. Empty means the default workspaces of master
    # admins (see app.pipelines.platform_ops.availability).
    OPS_WORKSPACE_IDS: str = ""
    # Content Guard reads text with a model and pictures with a vision model. Tests switch this off so a test run never
    # spends real provider quota; the guard's own tests switch it on and stub the models.
    CONTENT_GUARD_LIVE_CHECKS: bool = True


settings = Settings()