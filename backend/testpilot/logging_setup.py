"""Structured logging with secret redaction."""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone

from .config import get_settings

_REDACT = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)(\"?\s*[:=]\s*\"?)([^\s\",]+)"),
    re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^:\s]+:[^@\s]+@"),
]
_extra_secrets: set[str] = set()


def register_secret(value: str) -> None:
    if value and len(value) >= 4:
        _extra_secrets.add(value)


def redact(text: str) -> str:
    for s in sorted(_extra_secrets, key=len, reverse=True):
        text = text.replace(s, "••••")
    text = _REDACT[0].sub("sk-ant-••••", text)
    text = _REDACT[1].sub(lambda m: f"{m.group(1)}{m.group(2)}••••", text)
    text = _REDACT[2].sub("postgresql://••••:••••@", text)
    return text


class _Filter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


class _Json(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            out["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(out, ensure_ascii=False)


def setup(component: str) -> None:
    s = get_settings()
    h = logging.StreamHandler(sys.stdout)
    h.addFilter(_Filter())
    h.setFormatter(_Json() if s.log_json else logging.Formatter(f"%(asctime)s {component} %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(s.log_level.upper())
    for noisy in ("httpx", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
