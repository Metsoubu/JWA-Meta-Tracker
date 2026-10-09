"""Logging to a rotating file (always) and the console (when there is one)."""
from __future__ import annotations

import logging
import logging.handlers
import sys

from . import config

_configured = False


def setup_logging(console: bool = True, level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    config.log_dir().mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    file_handler = logging.handlers.RotatingFileHandler(
        config.log_file(), maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(file_handler)
    if console and sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        root.addHandler(stream)
    _configured = True
