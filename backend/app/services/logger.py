from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging() -> None:
    if logging.getLogger().handlers:
        return
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_path = os.getenv("AGENT_LOG_PATH", "").strip()
    if log_path:
        target = Path(log_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                target,
                maxBytes=1_000_000,
                backupCount=2,
                encoding="utf-8",
            )
        )
    logging.basicConfig(level=logging.INFO, format=formatter._fmt, handlers=handlers)
