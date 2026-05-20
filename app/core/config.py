"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central settings object populated from .env or environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )

    APP_NAME: str = "SaaS Backend"
    DEBUG: bool = False

    # Security
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # LLM provider — "groq" | "gemini" | "anthropic"
    LLM_PROVIDER: str = "groq"
    GROQ_API_KEY: str = ""
    GEMINI_API_KEY: str = ""

    # External services
    ANTHROPIC_API_KEY: str = ""
    STRIPE_SECRET_KEY: str = ""

    # Data stores
    MONGODB_URL: str
    REDIS_URL: str

    CLOUDINARY_CLOUD_NAME: str
    CLOUDINARY_API_KEY: str
    CLOUDINARY_API_SECRET: str


    # CORS — loaded from JSON array string in .env
    ALLOWED_ORIGINS: list[str] = ["http://localhost:3000"]


settings = Settings()
