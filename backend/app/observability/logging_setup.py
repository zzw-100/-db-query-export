"""Apply the configured log level to the application logger."""

import logging

from app.config import settings


def configure_logging() -> int:
    """Read log_level from settings and apply it. Returns the numeric level."""
    level_name = settings.log_level.upper()
    level = getattr(logging, level_name, logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO
    logging.getLogger("app").setLevel(level)
    return level
