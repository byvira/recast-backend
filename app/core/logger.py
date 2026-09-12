"""Centralized logging configuration.

Colored console output locally; one JSON object per line in production (via
structlog's JSONRenderer) — what Render's log drain / any aggregator needs.
Both carry the per-request request_id (and user_id/workspace_id once auth /
workspace resolution has run), bound via structlog.contextvars in
RequestLoggingMiddleware, app/core/auth.py, and app/core/workspace.py.
"""

import logging
import sys
from logging.config import dictConfig

import structlog

from app.core.config import settings


class RequestContextFilter(logging.Filter):
    """Copy structlog-bound contextvars onto every stdlib LogRecord.

    This is what lets a plain ``logging.getLogger(__name__).info(...)`` call
    anywhere in the app (or in a dependency like pymongo) carry request_id
    without every call site passing it explicitly.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = structlog.contextvars.get_contextvars()
        record.request_id = ctx.get("request_id", "-")
        record.user_id = ctx.get("user_id", "-")
        record.workspace_id = ctx.get("workspace_id", "-")
        return True


class JSONLogFormatter(logging.Formatter):
    """Render each LogRecord as one JSON object per line."""

    _renderer = structlog.processors.JSONRenderer()

    def format(self, record: logging.LogRecord) -> str:
        event = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "user_id": getattr(record, "user_id", "-"),
            "workspace_id": getattr(record, "workspace_id", "-"),
        }
        if record.exc_info:
            event["exc_info"] = self.formatException(record.exc_info)
        return self._renderer(None, None, event)


def setup_logging() -> None:
    """Configure application-wide logging."""

    log_level = "DEBUG" if settings.ENVIRONMENT != "production" else "INFO"

    log_format = settings.LOG_FORMAT or ("json" if settings.ENVIRONMENT == "production" else "console")
    formatter_name = "json" if log_format == "json" else "colored"

    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "request_context": {"()": RequestContextFilter},
            },
            "formatters": {
                "colored": {
                    "()": "colorlog.ColoredFormatter",
                    "format": (
                        "%(log_color)s"
                        "%(asctime)s | "
                        "%(levelname)-8s | "
                        "%(name)s | "
                        "req=%(request_id)s | "
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
                "json": {
                    "()": JSONLogFormatter,
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": formatter_name,
                    "filters": ["request_context"],
                    "stream": sys.stdout,
                },
            },
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
            "root": {
                "handlers": ["console"],
                "level": log_level,
            },
        }
    )


# Shared application logger
logger = logging.getLogger("app")
