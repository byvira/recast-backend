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
    DEBUG: bool = False

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
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/api/v1/oauth/google/callback"


    # Security
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # Auth — OTP rate limits and JWT configuration
    OTP_EXPIRE_MINUTES: int = 10
    OTP_MAX_ATTEMPTS: int = 5
    OTP_MAX_SENDS_PER_HOUR: int = 5
    OTP_MAX_SENDS_PER_DAY: int = 10
    OTP_COOLDOWN_SECONDS: int = 60
    OTP_LOCK_MINUTES: int = 15
    JWT_EXPIRE_HOURS: int = 24
    JWT_REFRESH_EXPIRE_DAYS: int = 30
    JWT_ISSUER: str = "saas-backend"
    JWT_AUDIENCE: str = "saas-api"

    # Email — Resend API
    RESEND_API_KEY: str = ""
    EMAIL_FROM: str = "noreply@yourdomain.com"

    # SMS — Twilio
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_PHONE_NUMBER: str = ""

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

    # External services
    STRIPE_SECRET_KEY: str = ""

    # Data stores
    MONGODB_URL: str
    REDIS_URL: str

    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""

    HUGGINGFACE_API_KEY: str = ""

    # Free tier credits limit
    FREE_CREDITS_LIMIT: int = 100

    # CORS — ALLOWED_ORIGINS loaded from JSON array string in .env (legacy / dev override)
    # In production, PRODUCTION_DOMAIN is used exclusively.
    ALLOWED_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # Production domain — required when ENVIRONMENT=production
    # Example: "https://app.yourdomain.com"
    PRODUCTION_DOMAIN: str = ""

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
    META_REDIRECT_URI: str = "https://YOUR-NGROK-URL.ngrok-free.app/api/v1/oauth/meta/callback"

    
    INSTAGRAM_APP_ID: str = ""
    INSTAGRAM_APP_SECRET: str = ""

    

    # ── LinkedIn ──────────────────────────────────────────────────────────
    # Register at: developer.linkedin.com/apps
    # Scopes needed: w_member_social, r_basicprofile
    LINKEDIN_CLIENT_ID: str = ""
    LINKEDIN_CLIENT_SECRET: str = ""
    LINKEDIN_REDIRECT_URI: str = "http://localhost:8000/api/v1/oauth/linkedin/callback"

    # ── Twitter / X ───────────────────────────────────────────────────────
    # Register at: developer.twitter.com/portal
    # Requires $100/mo Basic tier for write access (posting)
    # Scopes needed: tweet.read, tweet.write, users.read
    TWITTER_API_KEY: str = ""
    TWITTER_API_SECRET: str = ""
    TWITTER_BEARER_TOKEN: str = ""
    TWITTER_REDIRECT_URI: str = "http://localhost:8000/api/v1/oauth/twitter/callback"

    # ── Reddit ────────────────────────────────────────────────────────────
    # Register at: reddit.com/prefs/apps → create web app
    # Scopes needed: submit, identity, read
    REDDIT_CLIENT_ID: str = ""
    REDDIT_CLIENT_SECRET: str = ""
    REDDIT_REDIRECT_URI: str = "http://localhost:8000/api/v1/oauth/reddit/callback"
    # User-Agent format required by Reddit API — update version as needed
    REDDIT_USER_AGENT: str = "ViraStudio/1.0"
    BLUESKY_APP_NAME: str = "recast"
    META_CONFIG_ID: str = ""


    THREADS_APP_ID: str = ""
    THREADS_APP_SECRET: str = ""
    THREADS_REDIRECT_URI: str = ""

    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = ""
    JWT_ALGORITHM:str=""

    

    # ── Bluesky ───────────────────────────────────────────────────────────
    # No developer registration needed — uses AT Protocol auth.
    # Users connect via their handle + an App Password (not their main password).
    # App Passwords: bsky.app → Settings → Privacy and Security → App Passwords
    BLUESKY_SERVICE_URL: str = "https://bsky.social"

    # ── Publish Pipeline ──────────────────────────────────────────────────
    # Max retries before supervisor flags a post for human review
    PUBLISH_MAX_RETRIES: int = 3
    # Seconds to wait between retry attempts (base — multiplied per attempt)
    PUBLISH_RETRY_BASE_DELAY: int = 5
    # How many minutes ahead to refresh tokens before they expire
    TOKEN_REFRESH_THRESHOLD_MINUTES: int = 10080  # 7 days

  

settings = Settings()