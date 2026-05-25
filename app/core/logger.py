"""Centralized logging configuration with colored terminal output."""

import logging
import sys
from logging.config import dictConfig

from app.core.config import settings


def setup_logging() -> None:
    """Configure application-wide structured logging."""

    log_level = (
        "DEBUG"
        if settings.ENVIRONMENT != "production"
        else "INFO"
    )

    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,

            # ──────────────────────────────────────────────────────────────
            # Formatters
            # ──────────────────────────────────────────────────────────────
            "formatters": {
                "colored": {
                    "()": "colorlog.ColoredFormatter",
                    "format": (
                        "%(log_color)s"
                        "%(asctime)s | "
                        "%(levelname)-8s | "
                        "%(name)s | "
                        "%(message)s"
                    ),
                    "log_colors": {
                        "DEBUG": "cyan",
                        "INFO": "green",
                        "WARNING": "yellow",
                        "ERROR": "red",
                        "CRITICAL": "bold_red",
                    },
                },
            },

            # ──────────────────────────────────────────────────────────────
            # Handlers
            # ──────────────────────────────────────────────────────────────
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "colored",
                    "stream": sys.stdout,
                },
            },

            # ──────────────────────────────────────────────────────────────
            # Loggers
            # ──────────────────────────────────────────────────────────────
            "loggers": {
                "uvicorn": {
                    "handlers": ["console"],
                    "level": log_level,
                    "propagate": False,
                },

                "uvicorn.error": {
                    "handlers": ["console"],
                    "level": log_level,
                    "propagate": False,
                },

                "uvicorn.access": {
                    "handlers": ["console"],
                    "level": log_level,
                    "propagate": False,
                },

                "app": {
                    "handlers": ["console"],
                    "level": log_level,
                    "propagate": False,
                },
            },

            # ──────────────────────────────────────────────────────────────
            # Root Logger
            # ──────────────────────────────────────────────────────────────
            "root": {
                "handlers": ["console"],
                "level": log_level,
            },
        }
    )


# Shared application logger
logger = logging.getLogger("app")