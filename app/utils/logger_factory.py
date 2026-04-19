from __future__ import annotations

import logging
import os

_LOGGING_CONFIGURED = False
_DEFAULT_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
_DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(level: str | int | None = None) -> None:
    """Configure application-wide logging once."""
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return

    resolved_level: str | int = level if level is not None else os.getenv("APP_LOG_LEVEL", "DEBUG")
    if isinstance(resolved_level, str):
        resolved_level = resolved_level.strip().upper() or "DEBUG"

    logging.basicConfig(
        level=resolved_level,
        format=_DEFAULT_FORMAT,
        datefmt=_DEFAULT_DATE_FORMAT,
    )
    _LOGGING_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger instance by name."""
    configure_logging()
    return logging.getLogger(name)
