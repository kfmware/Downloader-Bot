import asyncio
import hashlib
import json
import logging
import os
import re
import sys
from contextlib import contextmanager, suppress
from contextvars import ContextVar, Token
from typing import Any, Iterator
from urllib.parse import urlsplit

import colorlog

_DISABLE_SINKS_ENV = "LOG_DISABLE_SINKS"


def _sinks_disabled() -> bool:
    return os.getenv(_DISABLE_SINKS_ENV, "").strip().lower() in {"1", "true", "yes"}


_log_context: ContextVar[dict[str, Any]] = ContextVar("maxload_log_context", default={})

_DEFAULT_TEXT_FIELDS = {
    "service": "-",
    "flow": "-",
    "request_id": "-",
    "event_name": "-",
    "kind": "app",
    "user_id": "-",
    "chat_type": "-",
    "task_name": "-",
    "duration_ms": "-",
    "text_context": "",
    "text_context_block": "",
    "text_message_block": "",
}

_STANDARD_RECORD_ATTRS = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}
_TEXT_RESERVED_FIELDS = _STANDARD_RECORD_ATTRS | {
    "service", "flow", "request_id", "event_name", "kind", "user_id",
    "chat_type", "task_name", "duration_ms", "text_context",
    "text_context_block", "text_message_block",
}
_TEXT_BASE_FIELD_ORDER = (
    "service", "flow", "request_id", "event_name", "user_id", "chat_type", "duration_ms",
)
_TEXT_DYNAMIC_FIELD_PRIORITY = (
    "bot_username", "entrypoint", "source", "url", "path", "file_id", "filename",
    "extra_filename", "size_hint", "size", "elapsed", "count", "workers",
    "max_workers", "queue_cap", "queue_depth", "priority", "position", "retry_after",
)
_TEXT_DYNAMIC_FIELD_MAX = 5
_TEXT_VALUE_MAX_LENGTH = 120
_LOG_HASH_LENGTH = 10
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_URL_PATH_NOISE = {"status", "statuses", "video", "watch", "reel", "reels", "p"}


def _sanitize_field_key(key: str) -> str:
    return f"extra_{key}" if key in _STANDARD_RECORD_ATTRS else key


def _sanitize_mapping(values: dict[str, Any]) -> dict[str, Any]:
    return {
        _sanitize_field_key(str(key)): value
        for key, value in values.items()
        if value not in (None, "")
    }


def _serialize_text_field_value(value: Any) -> str:
    if isinstance(value, str):
        rendered = value
        if len(rendered) > _TEXT_VALUE_MAX_LENGTH:
            rendered = f"{rendered[:_TEXT_VALUE_MAX_LENGTH - 1]}…"
        if rendered and all(ch not in rendered for ch in (' ', '"', "\n", "\r", "\t")):
            return rendered
        return json.dumps(rendered, ensure_ascii=False)
    if isinstance(value, (int, float, bool)):
        return str(value)
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(rendered) > _TEXT_VALUE_MAX_LENGTH:
        rendered = f"{rendered[:_TEXT_VALUE_MAX_LENGTH - 1]}…"
    return rendered


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:_LOG_HASH_LENGTH]


def summarize_url_for_log(url: Any) -> str:
    if not isinstance(url, str):
        return "-"
    candidate = url.strip()
    if not candidate:
        return "-"
    parsed = urlsplit(candidate if "://" in candidate else f"https://{candidate}")
    host = (parsed.netloc or "").lower() or "url"
    segments = [
        segment for segment in (parsed.path or "").split("/")
        if segment and segment.lower() not in _URL_PATH_NOISE
    ]
    hint = segments[-1][:24] if segments else "root"
    return f"{host}|{hint}|{_short_hash(candidate)}"


def summarize_text_for_log(text: Any) -> str:
    if not isinstance(text, str):
        return "-"
    candidate = text.strip()
    if not candidate:
        return "-"
    match = _URL_RE.search(candidate)
    if match:
        return summarize_url_for_log(match.group(0))
    return f"text|len={len(candidate)}|{_short_hash(candidate)}"


def _extract_dynamic_text_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value for key, value in record.__dict__.items()
        if key not in _TEXT_RESERVED_FIELDS and value not in (None, "", "-")
    }


def _is_meaningful_text_value(value: Any) -> bool:
    return value not in (None, "", "-")


def _ordered_dynamic_text_fields(dynamic_fields: dict[str, Any]) -> list[tuple[str, Any]]:
    priority_map = {key: index for index, key in enumerate(_TEXT_DYNAMIC_FIELD_PRIORITY)}
    return sorted(dynamic_fields.items(), key=lambda item: (priority_map.get(item[0], len(priority_map)), item[0]))


def _build_text_context(record: logging.LogRecord) -> str:
    fields: list[str] = []
    for key in _TEXT_BASE_FIELD_ORDER:
        value = getattr(record, key, None)
        if _is_meaningful_text_value(value):
            rendered_key = "event" if key == "event_name" else key
            fields.append(f"{rendered_key}={_serialize_text_field_value(value)}")
    dynamic_items = _ordered_dynamic_text_fields(_extract_dynamic_text_fields(record))
    for key, value in dynamic_items[:_TEXT_DYNAMIC_FIELD_MAX]:
        fields.append(f"{key}={_serialize_text_field_value(value)}")
    remaining = len(dynamic_items) - _TEXT_DYNAMIC_FIELD_MAX
    if remaining > 0:
        fields.append(f"more=+{remaining}")
    return " ".join(fields)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in _log_context.get().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        for key, default in _DEFAULT_TEXT_FIELDS.items():
            if not hasattr(record, key):
                setattr(record, key, default)
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        if getattr(record, "task_name", "-") in (None, "-", "") and task is not None:
            record.task_name = task.get_name()
        duration_ms = getattr(record, "duration_ms", None)
        if isinstance(duration_ms, (int, float)):
            record.duration_ms = f"{duration_ms:.2f}"
        elif duration_ms in (None, ""):
            record.duration_ms = "-"
        message = record.getMessage()
        if message.startswith("["):
            record.text_context_block = ""
            record.text_message_block = f" | {message}"
        else:
            record.text_context = _build_text_context(record)
            record.text_context_block = f" | {record.text_context}" if record.text_context else ""
            event_name = getattr(record, "event_name", None)
            if _is_meaningful_text_value(event_name) and message == event_name:
                record.text_message_block = ""
            else:
                record.text_message_block = f" | {message}" if message else ""
        return True


class LogContextManager:
    def __init__(self, **context: Any) -> None:
        self._context = _sanitize_mapping(context)
        self._token: Token | None = None

    def __enter__(self) -> dict[str, Any]:
        current = dict(_log_context.get())
        current.update(self._context)
        self._token = _log_context.set(current)
        return current

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._token is not None:
            _log_context.reset(self._token)


class ContextLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        merged_extra = _sanitize_mapping(dict(self.extra))
        extra = kwargs.get("extra")
        if extra:
            merged_extra.update(_sanitize_mapping(dict(extra)))
        kwargs["extra"] = merged_extra
        return msg, kwargs

    def bind(self, **extra: Any) -> "ContextLoggerAdapter":
        merged = _sanitize_mapping(dict(self.extra))
        merged.update(_sanitize_mapping(extra))
        return ContextLoggerAdapter(self.logger, merged)

    @contextmanager
    def context(self, **extra: Any) -> Iterator[dict[str, Any]]:
        with LogContextManager(**extra) as context:
            yield context

    def event(self, event_name: str, message: str | None = None, *, level: int = logging.INFO, **fields: Any) -> None:
        self.log(level, message or event_name, extra={"kind": "event", "event_name": event_name, **fields})

    def perf(self, event_name: str, *, duration_ms: float, message: str | None = None,
             level: int = logging.INFO, **fields: Any) -> None:
        self.log(level, message or event_name, extra={
            "kind": "perf", "event_name": event_name, "duration_ms": duration_ms, **fields,
        })

    def download_request(self, *, user_id: Any, username: str | None = None, service: str,
                         url: str, chat_type: str | None = None, **extra: Any) -> None:
        user_str = f"{user_id} (@{username})" if username else str(user_id)
        msg = f"[REQUEST] user={user_str} | service={service} | url={url}"
        self.info(msg, extra={"kind": "app", "user_id": user_id, "service": service, "url": url, **extra})

    def download_success(self, *, user_id: Any, service: str, file_type: str | None = "media",
                         size_bytes: int | None = None, duration_s: float | None = None,
                         url: str, **extra: Any) -> None:
        details: list[str] = []
        if size_bytes:
            details.append(_format_bytes_compact(size_bytes))
        if duration_s is not None:
            details.append(f"{duration_s:.2f}s")
        details_str = f" ({' in '.join(details)})" if details else ""
        ft = file_type or "media"
        msg = f"[SUCCESS] user={user_id} | {service} | {ft}{details_str} | {url}"
        self.info(msg, extra={"kind": "app", "user_id": user_id, "service": service, "url": url, **extra})

    def download_cached(self, *, user_id: Any, service: str, file_type: str | None = "media",
                        url: str, **extra: Any) -> None:
        ft = file_type or "media"
        msg = f"[CACHED] user={user_id} | {service} | {ft} (cache hit) | {url}"
        self.info(msg, extra={"kind": "app", "user_id": user_id, "service": service, "url": url, **extra})

    def download_error(self, *, user_id: Any, service: str, error: Any, url: str, **extra: Any) -> None:
        msg = f"[FAILED] user={user_id} | {service} | error={error} | {url}"
        self.error(msg, extra={"kind": "app", "user_id": user_id, "service": service, "url": url, **extra})


def _resolve_log_level(env_name: str, default: int = logging.INFO) -> int:
    val = os.getenv(env_name, "").strip().upper()
    return getattr(logging, val, default) if val else default


def _format_bytes_compact(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.2f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


CONSOLE_FORMAT = (
    "%(log_color)s%(asctime)s | %(levelname)s | %(name)s%(text_context_block)s%(text_message_block)s%(reset)s"
)


def _build_console_handler(level: int | None = None) -> logging.Handler:
    resolved_level = level if level is not None else _resolve_log_level("LOG_LEVEL", logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(resolved_level)
    formatter = colorlog.ColoredFormatter(
        CONSOLE_FORMAT,
        log_colors={"DEBUG": "cyan", "INFO": "green", "WARNING": "yellow", "ERROR": "red", "CRITICAL": "bold_red"},
    )
    handler.setFormatter(formatter)
    handler.addFilter(ContextFilter())
    return handler


_base_logger = logging.getLogger("maxload")
_base_logger.setLevel(logging.DEBUG)
_base_logger.propagate = False
_base_logger.handlers.clear()
if _sinks_disabled():
    _base_logger.addHandler(logging.NullHandler())
else:
    # Container-friendly: all application logs go to stdout; no directories or files are created.
    _base_logger.addHandler(_build_console_handler())

logger = ContextLoggerAdapter(_base_logger, {})


def set_log_context(**context: Any) -> Token:
    current = dict(_log_context.get())
    current.update(_sanitize_mapping(context))
    return _log_context.set(current)


def reset_log_context(token: Token) -> None:
    _log_context.reset(token)


def get_log_context() -> dict[str, Any]:
    return dict(_log_context.get())


_THIRD_PARTY_LOG_LEVELS = {
    "aiogram": logging.ERROR,
    "aiogram.event": logging.CRITICAL,
    "aiosqlite": logging.ERROR,
    "httpcore": logging.WARNING,
    "httpx": logging.WARNING,
    "asyncio": logging.WARNING,
}


def _configure_third_party_loggers() -> None:
    for name, level in _THIRD_PARTY_LOG_LEVELS.items():
        third_party_logger = logging.getLogger(name)
        third_party_logger.setLevel(level)
        third_party_logger.propagate = False
        # Route surviving third-party records through the same stdout handler.
        for handler in _base_logger.handlers:
            if handler not in third_party_logger.handlers:
                third_party_logger.addHandler(handler)


def disable_log_sinks() -> None:
    """Disable logging in short-lived worker subprocesses."""
    os.environ[_DISABLE_SINKS_ENV] = "1"
    targets = [_base_logger] + [logging.getLogger(name) for name in _THIRD_PARTY_LOG_LEVELS]
    for target in targets:
        for handler in list(target.handlers):
            target.removeHandler(handler)
            with suppress(Exception):
                handler.close()
    _base_logger.addHandler(logging.NullHandler())


_configure_third_party_loggers()
