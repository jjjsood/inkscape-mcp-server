"""`help`: orientation, intent lookup, op reference, runtime diagnostics, artifact stat."""

import difflib
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult

from inkscape_mcp.edit.op_docs import op_models, op_reference, ops_index
from inkscape_mcp.intents import detect_out_of_scope, match_intents
from inkscape_mcp.logging_setup import get_logger, log_file_io, log_tool_call
from inkscape_mcp.runtime.capabilities import get_cached_capabilities, refresh_capabilities
from inkscape_mcp.tools._result import guarded, ok
from inkscape_mcp.workspace.stat import stat_many

_logger = get_logger("tools.help")

Topic = Literal["capabilities", "diagnose", "ops"]

DESCRIPTION = (
    "Ask for guidance. goal='make the svg smaller' -> the tool:op to use + an example call; "
    "op='fill' -> that edit op's "
    "fields, risk and a valid example; topic='ops' lists every op, 'capabilities' shows what this "
    "runtime supports, 'diagnose' re-probes it; stat_paths=[...] returns size + sha256 of "
    "workspace files. No arguments: the short workflow. Example: help(op='create_rect')"
)

OVERVIEW = {
    "flow": "open -> edit(ops, render='inline') -> export",
    "tools": {
        "open": "open a file, svg text, blank canvas (or the live canvas); optional first ops",
        "inspect": "read structure, objects (with select), validate, quality, snapshots",
        "edit": "ONE call = many typed ops, atomic, one snapshot; $<n> refers to ids created "
        "by op n; dry_run shows the plan",
        "render": "look at the page, an object or a frame as an inline PNG",
        "export": "files (png/pdf/svg), profiles (web/icons/print), save_as",
        "history": "snapshots (list/create/restore/prune), restore_file (undo a save_as), reload",
        "help": "this tool",
        "live": "observe/steer a running Inkscape (when live mode is enabled)",
        "live_edit": "change the live selection (when live mode is enabled)",
    },
    "risk": "HIGH ops (delete, set_svg, insert_svg, path ops) need approval_token",
    "next": "help(topic='ops') lists the edit ops; help(op=NAME) shows one",
}


def _op_reference(name: str) -> dict[str, Any]:
    try:
        return op_reference(name)
    except KeyError:
        models = op_models()
        close = difflib.get_close_matches(name, models, n=3)
        hint = f" (similar: {', '.join(close)})" if close else ""
        raise ToolError(f"unknown op {name!r}{hint}; help(topic='ops') lists all ops") from None


def _capabilities(fresh: bool) -> dict[str, Any]:
    caps = refresh_capabilities() if fresh else get_cached_capabilities()
    data = caps.model_dump(mode="json", exclude_none=True)
    for noisy in ("tools", "tool_count", "intents"):
        data.pop(noisy, None)
    return data


@guarded(limit_msg="artifact exceeds the configured size limit")
def help_(
    goal: str | None = None,
    op: str | None = None,
    topic: Topic | None = None,
    stat_paths: list[str] | None = None,
) -> ToolResult:
    payload: dict[str, Any] = {}
    if goal is not None:
        rule = detect_out_of_scope(goal)
        if rule is not None:
            payload["goal"] = {"out_of_scope": True, "why": rule.reason}
        else:
            matches = match_intents(goal, limit=3)
            payload["goal"] = {
                "matches": [
                    {
                        "use": m.keys,
                        "how": m.how_to,
                        "example": m.example.model_dump() if m.example else None,
                    }
                    for m in matches
                ]
                or "no confident match; try help(topic='ops') or help()"
            }
    if op is not None:
        payload["op"] = _op_reference(op)
    if topic == "ops":
        payload["ops"] = ops_index()
    elif topic in ("capabilities", "diagnose"):
        payload["capabilities"] = _capabilities(fresh=topic == "diagnose")
    if stat_paths is not None:
        if not stat_paths:
            raise ToolError("stat_paths needs at least one path")
        stats = stat_many(stat_paths)
        log_file_io(_logger, action="stat", count=stats.count)
        payload["stat"] = {
            "artifacts": [s.model_dump() for s in stats.artifacts],
            "total_bytes": stats.total_bytes,
        }
    if not payload:
        payload = dict(OVERVIEW)
    log_tool_call(_logger, tool="help", parts=sorted(payload))
    return ok(payload)


def register(mcp: FastMCP) -> None:
    mcp.tool(name="help", description=DESCRIPTION, output_schema=None)(help_)
