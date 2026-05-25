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


settings = Settings()
