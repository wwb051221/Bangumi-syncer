"""Logging helpers: human readable text or one JSON object per line."""

from __future__ import annotations

import json
import logging
import os
import sys
import time


class JsonFormatter(logging.Formatter):
    """Render each record as a single JSON line (container friendly)."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        base = "%s %-5s %-22s %s" % (ts, record.levelname, record.name, record.getMessage())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    """Configure the root logger once; repeated calls only adjust the level."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    handler = None
    for h in root.handlers:
        if getattr(h, "_fnos2bgm", False):
            handler = h
            break
    if handler is None:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler._fnos2bgm = True  # type: ignore[attr-defined]
        root.addHandler(handler)

    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())

    # urllib3/paramiko style noise is irrelevant here, but keep explicit.
    logging.getLogger("urllib").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger("fnos2bgm." + name if not name.startswith("fnos2bgm") else name)


def debug_enabled() -> bool:
    return os.environ.get("FNOS2BGM_DEBUG", "") not in ("", "0", "false")
