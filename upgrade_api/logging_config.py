"""Centralized logging configuration for the upgrade API.

Call setup_logging() once at startup (in main.py).
All loggers across upgrade_api and upgrade_lib then write to the same
rotating file at logs/yantrix.log so both backend errors and individual
agent/provider traces end up in one place.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path


LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOG_DIR / "yantrix.log"

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per file
_BACKUP_COUNT = 5               # keep last 5 rotated files


def setup_logging(level: int = logging.DEBUG) -> None:
    """Configure the root logger with a rotating file handler + console handler.

    Safe to call multiple times — subsequent calls are no-ops if handlers are
    already attached.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    if root.handlers:
        return  # already configured (e.g. uvicorn --reload reuses the process)

    root.setLevel(level)
    fmt = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    # Rotating file — every error, warning, debug goes here
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)
    root.addHandler(file_handler)

    # Console — INFO and above (keeps the terminal readable)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    console_handler.setLevel(logging.INFO)
    root.addHandler(console_handler)

    # Quiet down chatty third-party libraries
    for noisy in ("httpx", "httpcore", "git", "urllib3", "botocore", "boto3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
