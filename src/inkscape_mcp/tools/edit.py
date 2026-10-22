"""`edit`: one call, many typed ops, one atomic recorded edit (replaces every old edit tool)."""

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from pydantic import Field

from inkscape_mcp.edit.op_docs import op_signatures
from inkscape_mcp.edit.ops import MAX_OPS, OpsResult, run_ops
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.tools._result import guarded, inline_png, ok

_logger = get_logger("tools.edit")

Render = Literal["none", "inline"]
Detail = Literal["ids", "summary", "full"]

DESCRIPTION = (
    "Change a document with typed ops in ONE atomic call (all or nothing; snapshot_id = state "
    "BEFORE it; undo with history): create shapes, restyle, move, group, text, compose, "
    "optimize. Refer to an "
    "op's new object as '$<op index>'; target existing ones with ids=[...] or select={tag,fill,"
    "...}. render='inline' returns a PNG; dry_run=true the plan. HIGH ops (delete, set_svg, "
    "insert_svg, path ops) need approval_token. help(op=NAME) shows fields. "
    "Example: edit(doc_id, ops=[{'op':'create_rect','x':0,'y':0,'width':200,'height':100,"
    "'fill':'#36f','id':'bg'},{'op':'fill','ids':['$0'],'color':'#f63'}], render='inline')"
)

OPS_PARAM = (
    f"1..{MAX_OPS} ops, each {{'op': NAME, ...fields}}; put every step of a task in ONE call. "
    "Ops and fields (?=optional): "
    + op_signatures()
    + ". Path ops (simplify..cleanup) need advanced mode. Examples: help(op=NAME)."
)


def ops_payload(res: OpsResult, detail: Detail) -> dict[str, Any]:
    """The compact result of an op run at the requested detail level."""
    out: dict[str, Any] = {
        "doc_id": res.doc_id,
        "changed": res.changed,
        "snapshot_id": res.snapshot_id,
        "risk": res.risk,
        "created": [{"op": c.op, "id": c.id, "bbox": c.bbox} for c in res.created],
        "warnings": res.warnings,
    }
    if res.dry_run:
        out["dry_run"] = True
        out["needs_approval"] = res.needs_approval or None
        out["plan"] = [
            {"op": o.op, "name": o.name, "matched": o.matched, "creates": o.created_ids}
            for o in res.outcomes
        ]
    elif res.needs_approval:
        out["needs_approval"] = True
        if res.changed and res.snapshot_id:
            out["undo"] = f"history(doc_id, action='restore', snapshot_id='{res.snapshot_id}')"
    if detail in ("summary", "full") and res.changed and res.snapshot_id:
        out["snapshot_note"] = (
            "snapshot_id is the state BEFORE this edit; restoring it undoes this edit "
            "(and any later ones)"
        )
    if detail in ("summary", "full") and not res.dry_run:
        out["ops"] = [f"{o.op} {o.name}: {o.summary}" for o in res.outcomes]
    if detail == "full":
        out["operation_id"] = res.operation_id
        out["summary"] = res.summary
        out["outcomes"] = [o.model_dump(mode="json") for o in res.outcomes]
    return out


@guarded()
def edit(
    doc_id: str,
    ops: Annotated[list[dict[str, Any]], Field(description=OPS_PARAM)],
    render: Render = "none",
    dry_run: bool = False,
    detail: Detail = "ids",
    approval_token: str | None = None,
) -> ToolResult:
    res = run_ops(
        doc_id,
        ops,
        dry_run=dry_run,
        render=render == "inline" and not dry_run,
        approval_token=approval_token,
    )
    log_tool_call(
        _logger,
        tool="edit",
        doc_id=doc_id,
        ops=len(ops),
        risk=res.risk,
        dry_run=dry_run,
        changed=res.changed,
    )
    return ok(ops_payload(res, detail), inline_png(doc_id, res.render_artifact))


def register(mcp: FastMCP) -> None:
    mcp.tool(name="edit", description=DESCRIPTION, output_schema=None)(edit)
