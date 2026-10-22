"""`actions` (advanced mode): the controlled Inkscape Action surface — discovery, validation, and
approval-gated execution of an allowlisted chain or one raw Action.
"""

from pathlib import Path
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from lxml import etree

from inkscape_mcp.actions import chains as chain_engine
from inkscape_mcp.actions.capability_map import (
    discover_actions,
    discover_extensions,
)
from inkscape_mcp.actions.chains import ActionStep
from inkscape_mcp.config import get_settings
from inkscape_mcp.edit.pipeline import apply_edit
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.registry import get_registry
from inkscape_mcp.tools._result import guarded, ok, require_doc
from inkscape_mcp.workspace.risk import RiskClass

_logger = get_logger("tools.actions")

Action = Literal["list", "extensions", "validate", "run", "raw"]

DESCRIPTION = (
    "Advanced escape hatch: allowlisted Inkscape Actions. action='list' shows what this runtime "
    "exposes (all=false omits the id array), 'extensions' the allowlisted extensions, "
    "'validate' checks `steps` [{action, args[]}] without running, 'run' executes the chain on "
    "doc_id, 'raw' runs ONE step (dry_run defaults to true). Running is HIGH risk: needs "
    "approval_token. Prefer edit ops when one exists. "
    "Example: actions(action='run', doc_id=d, steps=[{'action':'select-all'}], approval_token='ok')"
)


def _steps(steps: list[dict[str, Any]] | None) -> list[ActionStep]:
    if not steps:
        raise ToolError("steps must contain at least one {action, args} step")
    return [ActionStep(**s) for s in steps]


def _run_chain(
    doc_id: str, tool: str, chain: list[ActionStep], approval_token: str | None
) -> dict[str, Any]:
    plan = chain_engine.validate_chain(chain)

    def mutate(tree: etree._ElementTree) -> str:
        entry = get_registry().get(doc_id)
        return chain_engine.run_chain_op(tree, Path(entry.working_path), chain)

    result = apply_edit(
        doc_id,
        tool,
        {"steps": [s.model_dump() for s in chain], "actions": plan.actions_argument},
        mutate,
        approval_token=approval_token,
        risk_class=RiskClass.HIGH,
    )
    return {
        "doc_id": doc_id,
        "changed": result.changed,
        "snapshot_id": result.snapshot_id,
        "operation_id": result.operation_id,
        "summary": result.summary,
        "plan": plan,
    }


@guarded()
def actions(
    action: Action,
    steps: list[dict[str, Any]] | None = None,
    doc_id: str | None = None,
    all: bool = True,
    dry_run: bool | None = None,
    approval_token: str | None = None,
) -> ToolResult:
    payload: Any
    if action == "list":
        payload = discover_actions()
        if not all:
            payload = payload.model_copy(update={"actions": []})
    elif action == "extensions":
        payload = discover_extensions()
    elif action == "validate":
        payload = chain_engine.validate_chain(_steps(steps))
    elif action == "run":
        if not doc_id:
            raise ToolError("action='run' requires doc_id")
        require_doc(doc_id)
        if not (approval_token and approval_token.strip()):
            raise ToolError("running an action chain is HIGH risk; re-call with approval_token")
        payload = _run_chain(doc_id, "actions.run", _steps(steps), approval_token)
    else:  # raw
        if not get_settings().raw_action_enabled:
            raise ToolError(
                "raw_action_disabled: raw actions are disabled "
                "(set INKSCAPE_MCP_RAW_ACTION_ENABLED to enable advanced mode)"
            )
        if not doc_id:
            raise ToolError("action='raw' requires doc_id")
        require_doc(doc_id)
        chain = _steps(steps)
        if len(chain) != 1:
            raise ToolError("action='raw' takes exactly one step; use action='run' for a chain")
        preview = True if dry_run is None else dry_run
        if not preview and not (approval_token and approval_token.strip()):
            raise ToolError("a raw action is HIGH risk; re-call with approval_token")
        if preview:
            payload = {
                "doc_id": doc_id,
                "dry_run": True,
                "changed": False,
                "plan": chain_engine.validate_chain(chain),
            }
        else:
            payload = _run_chain(doc_id, "actions.raw", chain, approval_token)
    log_tool_call(_logger, tool="actions", action=action, doc_id=doc_id)
    return ok(payload)


def register(mcp: FastMCP) -> None:
    mcp.tool(name="actions", description=DESCRIPTION, output_schema=None)(actions)
