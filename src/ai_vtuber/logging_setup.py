from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

REDACTED = "***REDACTED***"
_SENSITIVE_KEY_PARTS = (
    "token",
    "authorization",
    "password",
    "secret",
    "credential",
)
_SENSITIVE_KEYS = ("device_code",)
_AUTH_PATTERN = re.compile(r"(?i)\b(?:Bearer|OAuth)\s+[A-Za-z0-9._~+/=-]+")
_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(authenticationToken|access_token|refresh_token|device_code|token|"
    r"authorization|password|secret)\b(\s*[:=]\s*)([^\s,;]+)"
)


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).casefold()
    return normalized in _SENSITIVE_KEYS or any(
        part in normalized for part in _SENSITIVE_KEY_PARTS
    )


def redact(value: object, key: object | None = None) -> object:
    if key is not None and _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(item_key): redact(item, item_key) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = _AUTH_PATTERN.sub(REDACTED, value)
        return _ASSIGNMENT_PATTERN.sub(
            lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}",
            value,
        )
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        event_data = getattr(record, "event_data", None)
        if isinstance(event_data, dict):
            payload["data"] = redact(event_data)
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str) -> logging.Logger:
    logger = logging.getLogger("ai_vtuber")
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    **event_data: object,
) -> None:
    logger.log(level, event, extra={"event_data": event_data})
