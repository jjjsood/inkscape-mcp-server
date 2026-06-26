"""Structured logging (architecture §5).

Logs are JSON-lines on STDERR. They MUST NOT go to stdout: stdout is the MCP STDIO protocol
channel and any stray write there corrupts the wire. `configure_logging()` is idempotent and
installs a single stderr handler that serializes each record (plus any structured extra
fields) as one JSON object per line.

Event emitters cover the event classes — each writes a record with an `event` field:
tool_call, policy_decision, file_io, process_exec, export, preview, error, approval.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

LOGGER_NAMESPACE = "inkscape_mcp"

# Standard LogRecord attributes excluded when collecting structured `extra` fields.
_RESERVED_LOGRECORD_KEYS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
    }
)

# Valid event classes.
EVENT_TOOL_CALL = "tool_call"
EVENT_POLICY_DECISION = "policy_decision"
EVENT_FILE_IO = "file_io"
EVENT_PROCESS_EXEC = "process_exec"
EVENT_EXPORT = "export"
EVENT_PREVIEW = "preview"
EVENT_ERROR = "error"
EVENT_APPROVAL = "approval"


class _JsonLinesFormatter(logging.Formatter):
    """Serialize each LogRecord (plus structured extras) as a single JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOGRECORD_KEYS or key.startswith("_"):
                continue
            if key in payload:
                continue
            payload[key] = _coerce_jsonable(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def _coerce_jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value


def configure_logging() -> None:
    """Idempotently install the JSON-lines stderr handler on the package logger.

    Logs go to STDERR only — never stdout (the MCP STDIO channel). Re-calling is a no-op once
    the handler is present.
    """
    logger = logging.getLogger(LOGGER_NAMESPACE)
    for handler in logger.handlers:
        if getattr(handler, "_inkscape_mcp_json", False):
            return
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(_JsonLinesFormatter())
    handler._inkscape_mcp_json = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the package namespace (configures logging on first use)."""
    configure_logging()
    if name == LOGGER_NAMESPACE or name.startswith(LOGGER_NAMESPACE + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_NAMESPACE}.{name}")


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Emit a structured record carrying an `event` field plus arbitrary machine fields."""
    level = logging.ERROR if event == EVENT_ERROR else logging.INFO
    logger.log(level, event, extra={"event": event, **fields})


def log_tool_call(logger: logging.Logger, **fields: Any) -> None:
    """Emit a `tool_call` event."""
    log_event(logger, EVENT_TOOL_CALL, **fields)


def log_policy_decision(logger: logging.Logger, **fields: Any) -> None:
    """Emit a `policy_decision` event."""
    log_event(logger, EVENT_POLICY_DECISION, **fields)


def log_file_io(logger: logging.Logger, **fields: Any) -> None:
    """Emit a `file_io` event."""
    log_event(logger, EVENT_FILE_IO, **fields)


def log_process_exec(logger: logging.Logger, **fields: Any) -> None:
    """Emit a `process_exec` event."""
    log_event(logger, EVENT_PROCESS_EXEC, **fields)


def log_export(logger: logging.Logger, **fields: Any) -> None:
    """Emit an `export` event."""
    log_event(logger, EVENT_EXPORT, **fields)


def log_preview(logger: logging.Logger, **fields: Any) -> None:
    """Emit a `preview` event."""
    log_event(logger, EVENT_PREVIEW, **fields)


def log_error(logger: logging.Logger, **fields: Any) -> None:
    """Emit an `error` event (logged at ERROR level)."""
    log_event(logger, EVENT_ERROR, **fields)


def log_approval(logger: logging.Logger, **fields: Any) -> None:
    """Emit an `approval` event."""
    log_event(logger, EVENT_APPROVAL, **fields)
