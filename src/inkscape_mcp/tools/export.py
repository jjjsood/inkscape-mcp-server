"""`export`: write results out. Files, profiles (web / icons / print) and save-as, for one document
or a set, in one call.
"""

from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from pydantic import BaseModel, ConfigDict, Field

from inkscape_mcp import quality as quality_engine
from inkscape_mcp.config import resolve_settings
from inkscape_mcp.document.save import save_document_as
from inkscape_mcp.logging_setup import get_logger, log_export
from inkscape_mcp.render.batch import ExportSpec, export_batch
from inkscape_mcp.render.cli import discard_on_error
from inkscape_mcp.render.inline import resolve_artifact_abspath
from inkscape_mcp.render.profiles import (
    ProfileResult,
    create_icon_set,
    export_print_profile,
    export_web_profile,
    profile_specs,
)
from inkscape_mcp.tools._result import guarded, ok, require_doc, to_tool_error

_logger = get_logger("tools.export")

LIMIT_MSG = "export exceeds the configured size or dimension limit"


class FileTarget(BaseModel):
    """One file: `format` png | pdf | svg; `object_id` clips to one object; `width_px` (png)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["file"]
    format: str
    object_id: str | None = None
    width_px: int | None = None


class ProfileTarget(BaseModel):
    """A ready-made set. web: responsive PNGs + SVG (`width_px` | `widths` | `scales`); icons:
    square PNGs (`sizes`, default 16..256); print: one print-ready PDF."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["profile"]
    name: Literal["web", "icons", "print"]
    width_px: int | None = None
    widths: list[int] | None = None
    scales: list[int] | None = None
    sizes: list[int] | None = None


class SaveAsTarget(BaseModel):
    """Write the working copy to a workspace file. Overwriting needs overwrite + approval_token.

    `dest_path` is workspace-relative; `out_dir` does not apply to it.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["save_as"]
    dest_path: str
    overwrite: bool = False


Target = Annotated[FileTarget | ProfileTarget | SaveAsTarget, Field(discriminator="kind")]

DESCRIPTION = (
    "Write results to files in the workspace. `targets` is a list of: "
    "{kind:'file', format:'png|pdf|svg', object_id?, width_px?}, "
    "{kind:'profile', name:'web|icons|print', ...} or {kind:'save_as', dest_path, overwrite?} "
    "(dest_path workspace-relative; out_dir: file/profile only, defaults to save_as's folder). "
    "doc_id may be a list to export a set (files are prefixed with each doc_id). dry_run=true "
    "plans and projects sizes without writing; byte_budget caps each doc's total. Returns each "
    "artifact's workspace path, size and pixel dimensions. "
    "Example: export(doc_id, targets=[{'kind':'file','format':'png','width_px':1024},"
    "{'kind':'file','format':'svg'}])"
)


def _bytes(doc_id: str, relative_path: str | None) -> int | None:
    if not relative_path:
        return None
    path = resolve_artifact_abspath(doc_id, relative_path)
    return path.stat().st_size if path is not None else None


def _artifact(doc_id: str, kind: str, path: str | None, fmt: str, **extra: Any) -> dict[str, Any]:
    return {
        "doc_id": doc_id,
        "kind": kind,
        "path": path,
        "format": fmt,
        "bytes": _bytes(doc_id, path),
        **extra,
    }


def _profile(
    doc_id: str, t: ProfileTarget, out_dir: str | None, prefix: str | None
) -> ProfileResult:
    if t.name == "web":
        kwargs: dict[str, Any] = {"widths": t.widths, "scales": t.scales}
        if t.width_px is not None:
            kwargs["width_px"] = t.width_px
        return export_web_profile(doc_id, out_dir=out_dir, name_prefix=prefix, **kwargs)
    if t.name == "icons":
        return create_icon_set(doc_id, sizes=t.sizes, out_dir=out_dir, name_prefix=prefix)
    return export_print_profile(doc_id, out_dir=out_dir, name_prefix=prefix)


def _plan_one(
    doc_id: str,
    targets: list[Target],
    *,
    out_dir: str | None,
    prefix: str | None,
    byte_budget: int | None,
) -> dict[str, Any]:
    """Validate every target and project the whole call's output size, writing nothing.

    File targets are projected as one batch and each profile through its `profile_specs`, so a real
    run can be refused BEFORE any write when the call as a whole (files + profiles) would exceed the
    byte budget. The budget is clamped ONCE per document (a caller may only lower the configured
    per-document artifact cap); with several doc_ids each document gets its own budget.
    """
    cap = resolve_settings(None).artifact_max_bytes_per_doc
    budget = min(byte_budget, cap) if byte_budget is not None else cap
    result: dict[str, Any] = {
        "artifacts": [],
        "plan": [],
        "projected_bytes": 0,
        "within_budget": True,
    }
    files = [t for t in targets if isinstance(t, FileTarget)]
    batch_done = False
    for index, target in enumerate(targets):
        try:
            if isinstance(target, FileTarget):
                if batch_done:
                    continue
                batch_done = True
                specs = [
                    ExportSpec(format=f.format, width_px=f.width_px, object_id=f.object_id)
                    for f in files
                ]
                batch = export_batch(
                    doc_id,
                    specs,
                    dry_run=True,
                    byte_budget=byte_budget,
                    out_dir=out_dir,
                    name_prefix=prefix,
                )
                result["plan"].extend(
                    {
                        "format": i.format,
                        "width_px": i.width_px,
                        "object_id": i.object_id,
                        "projected_bytes": i.projected_bytes,
                    }
                    for i in batch.items
                )
            elif isinstance(target, ProfileTarget):
                specs = profile_specs(
                    target.name,
                    width_px=target.width_px,
                    widths=target.widths,
                    scales=target.scales,
                    sizes=target.sizes,
                )
                batch = export_batch(
                    doc_id,
                    specs,
                    dry_run=True,
                    byte_budget=byte_budget,
                    out_dir=out_dir,
                    name_prefix=prefix,
                )
                result["plan"].extend(
                    {
                        "profile": target.name,
                        "format": i.format,
                        "width_px": i.width_px,
                        "projected_bytes": i.projected_bytes,
                    }
                    for i in batch.items
                )
            else:
                result["plan"].append(
                    {"save_as": target.dest_path, "overwrite": target.overwrite or None}
                )
                continue
            # One budget for this document's whole call (files + every profile).
            result["byte_budget"] = budget
            result["projected_bytes"] += batch.projected_total_bytes
            result["within_budget"] = result["projected_bytes"] <= budget
        except Exception as exc:
            reason = to_tool_error(exc, limit_msg=LIMIT_MSG)
            raise ToolError(f"{doc_id} targets[{index}] ({target.kind}): {reason}") from exc
    return result


def _export_one(
    doc_id: str,
    targets: list[Target],
    *,
    out_dir: str | None,
    prefix: str | None,
    byte_budget: int | None,
    dry_run: bool,
    approval_token: str | None,
) -> dict[str, Any]:
    plan = _plan_one(doc_id, targets, out_dir=out_dir, prefix=prefix, byte_budget=byte_budget)
    if dry_run:
        return plan
    if not plan["within_budget"]:
        # Same refusal export_batch gives, but for files + profiles together, before any write.
        raise ToolError(
            f"{doc_id}: export batch projected output exceeds the byte budget "
            f"({plan['projected_bytes']} > {plan['byte_budget']} bytes)"
        )
    result: dict[str, Any] = {"artifacts": []}
    files = [t for t in targets if isinstance(t, FileTarget)]
    batch_done = False
    for index, target in enumerate(targets):
        try:
            if isinstance(target, FileTarget):
                if batch_done:
                    continue
                batch_done = True
                specs = [
                    ExportSpec(format=f.format, width_px=f.width_px, object_id=f.object_id)
                    for f in files
                ]
                batch = export_batch(
                    doc_id,
                    specs,
                    dry_run=False,
                    byte_budget=byte_budget,
                    out_dir=out_dir,
                    name_prefix=prefix,
                )
                result["artifacts"].extend(
                    _artifact(
                        doc_id,
                        "file",
                        i.workspace_relative_path,
                        i.format,
                        width_px=i.width_px,
                        object_id=i.object_id,
                    )
                    for i in batch.items
                )
            elif isinstance(target, ProfileTarget):
                prof = _profile(doc_id, target, out_dir, prefix)
                result["artifacts"].extend(
                    _artifact(
                        doc_id,
                        f"profile:{target.name}",
                        a.workspace_relative_path,
                        a.format,
                        width_px=a.width_px,
                        height_px=a.height_px,
                    )
                    for a in prof.artifacts
                )
            else:
                saved = save_document_as(
                    doc_id,
                    target.dest_path,
                    overwrite=target.overwrite,
                    approval_token=approval_token,
                )
                result["saved"] = {
                    "path": saved.saved_path,
                    "bytes": saved.bytes,
                    "overwritten": saved.overwritten or None,
                    "operation_id": saved.operation_id,
                }
            # Backstop on ACTUAL bytes across files + profiles (the projection may under-estimate):
            # abort once this document's real output crosses its budget; the caller's
            # discard_on_error block then removes everything this call wrote.
            actual = sum(a["bytes"] or 0 for a in result["artifacts"])
            if "byte_budget" in plan and actual > plan["byte_budget"]:
                raise ToolError(
                    f"export output exceeded the byte budget mid-run "
                    f"({actual} > {plan['byte_budget']} bytes)"
                )
        except Exception as exc:
            if isinstance(target, FileTarget):
                # All file targets run as one batch at the first one; name the spec that failed.
                file_positions = [i for i, t in enumerate(targets) if isinstance(t, FileTarget)]
                index = file_positions[getattr(exc, "export_failed_index", 0)]
            reason = to_tool_error(exc, limit_msg=LIMIT_MSG)
            raise ToolError(f"{doc_id} targets[{index}] ({target.kind}): {reason}") from exc
    return result


@guarded(limit_msg=LIMIT_MSG)
def export(
    doc_id: str | list[str],
    targets: list[Target],
    out_dir: str | None = None,
    name_prefix: str | None = None,
    byte_budget: int | None = None,
    dry_run: bool = False,
    approval_token: str | None = None,
) -> ToolResult:
    if not targets:
        raise ToolError("targets must contain at least one target")
    ids = [doc_id] if isinstance(doc_id, str) else list(doc_id)
    if not ids:
        raise ToolError("doc_id must name at least one document")
    multi = not isinstance(doc_id, str)
    if multi:
        quality_engine.require_unique_doc_ids(ids)
    for one in ids:  # fail on an unknown id before writing anything
        require_doc(one)
    if multi and any(isinstance(t, SaveAsTarget) for t in targets):
        raise ToolError("save_as writes one document; call export once per doc_id for save_as")
    # No out_dir but a save_as in the same call: put file/profile outputs next to the saved file
    # instead of the hidden server store.
    save_as = next((t for t in targets if isinstance(t, SaveAsTarget)), None)
    defaulted_out_dir = out_dir is None and save_as is not None
    if defaulted_out_dir and save_as is not None:
        out_dir = Path(save_as.dest_path).parent.as_posix()

    # All-or-nothing for exported files: a failing target (or document) removes every file/profile
    # artifact this call already wrote, so no half result is left behind. A completed save_as is
    # kept: it is its own Operation Record.
    written: list[Path] = []
    try:
        with discard_on_error() as written:
            per_doc = {
                one: _export_one(
                    one,
                    targets,
                    out_dir=out_dir,
                    prefix=name_prefix if name_prefix is not None else (one if multi else None),
                    byte_budget=byte_budget,
                    dry_run=dry_run,
                    approval_token=approval_token,
                )
                for one in ids
            }
    except ToolError as exc:
        if written:
            raise ToolError(
                f"{exc}; {len(written)} partial artifact(s) of this call were removed"
            ) from exc
        raise
    artifacts = [a for r in per_doc.values() for a in r["artifacts"]]
    payload: dict[str, Any] = {"artifacts": artifacts}
    if defaulted_out_dir and artifacts:
        payload["out_dir"] = f"{out_dir} (defaulted to the save_as folder)"
    if dry_run:
        payload = {
            "dry_run": True,
            "plan": {one: r["plan"] for one, r in per_doc.items()}
            if multi
            else per_doc[ids[0]]["plan"],
            "projected_bytes": sum(r.get("projected_bytes", 0) for r in per_doc.values()),
            "projected_note": "upper bound (raw RGBA); real PNG files are much smaller",
            "within_budget": all(r.get("within_budget", True) for r in per_doc.values()),
        }
    else:
        saved = {one: r["saved"] for one, r in per_doc.items() if "saved" in r}
        payload["count"] = len(artifacts) + len(saved)
        payload["total_bytes"] = sum(a["bytes"] or 0 for a in artifacts) + sum(
            s["bytes"] for s in saved.values()
        )
        if saved:
            payload["saved"] = saved[ids[0]] if not multi else saved
    log_export(_logger, tool="export", docs=len(ids), targets=len(targets), dry_run=dry_run)
    return ok(payload)


def register(mcp: FastMCP) -> None:
    mcp.tool(name="export", description=DESCRIPTION, output_schema=None)(export)
