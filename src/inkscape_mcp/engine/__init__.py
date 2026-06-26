"""Headless `inkscape --shell` engine layer (ADR-007).

A warm, supervised, opt-in alternative transport for the Inkscape-engine ops (render / export /
path / boolean / action-chain): one long-lived `inkscape --shell` worker per document instead of a
fresh `inkscape …` process per call. Gated by `INKSCAPE_MCP_ENGINE_MODE=shell`; always carries an
automatic per-call CLI fallback so it can never regress correctness. A PRIVATE headless worker — NOT
a channel to the user's live GUI (architecture §4.4).
"""

from __future__ import annotations

from inkscape_mcp.engine.manager import (
    EngineManager,
    get_engine_manager,
    reset_engine_manager,
)
from inkscape_mcp.engine.ops import (
    ENGINE_EXPORT_FORMATS,
    engine_export_document,
    engine_mode_is_shell,
    engine_run_actions,
)
from inkscape_mcp.engine.process import (
    EngineActionError,
    EngineCrash,
    EngineError,
    EngineProcess,
    EngineResponse,
    EngineTimeout,
    EngineUnavailable,
    shell_mode_available,
)

__all__ = [
    "ENGINE_EXPORT_FORMATS",
    "EngineActionError",
    "EngineCrash",
    "EngineError",
    "EngineManager",
    "EngineProcess",
    "EngineResponse",
    "EngineTimeout",
    "EngineUnavailable",
    "engine_export_document",
    "engine_mode_is_shell",
    "engine_run_actions",
    "get_engine_manager",
    "reset_engine_manager",
    "shell_mode_available",
]
