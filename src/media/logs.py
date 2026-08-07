"""File logging at ~/.local/share/media/logs/media.log."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import LOG_DIR

LOGGER_NAME = "media"
_configured = False


def setup_logging(verbose: bool = False, log_dir: Path | None = None) -> logging.Logger:
    """Attach a rotating file handler. Safe to call more than once."""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if _configured:
        return logger

    directory = log_dir or LOG_DIR
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            directory / "media.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        handler.setLevel(logging.DEBUG)
        logger.addHandler(handler)
    except OSError:
        # A read-only home directory must never stop a download.
        logger.addHandler(logging.NullHandler())

    logger.propagate = False
    _configured = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(LOGGER_NAME if name is None else f"{LOGGER_NAME}.{name}")


def log_path() -> Path:
    return LOG_DIR / "media.log"
