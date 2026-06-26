"""Export-profile tools: `export_web_profile`, `create_icon_set`, `export_print_profile`.

Thin MCP layer over the profile engine (`inkscape_mcp.render.profiles`), which itself composes the
CLI render/export engine. Inkscape engine per ADR-005. Following ADR-002, the three profiles
are discrete, typed tools (NOT a single `profile: str` string-dispatch portmanteau).

All three are risk class **low**: they write only to the per-document artifact / exports dir and
never overwrite the original or the working copy, so no Operation Record or snapshot is required
(same as the export tools).

Client-facing errors are raised as `ToolError` with stable, host-path-free messages (sec.12):
unknown document id -> "document id not found"; a DISTINGUISHABLE icon/web size cause (PF5)
-> "icon size must be a positive integer" (<=0) vs "icon size exceeds the configured pixel cap"
(over-cap); limit / process / render failures -> stable messages. Returned artifact paths follow ONE
LOCATION CONTRACT: `path` and `workspace_relative_path` carry the SAME value, always
relative to the WORKSPACE ROOT, openable by a single join to the root — never an absolute host path.
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError
from pydantic import BaseModel

from inkscape_mcp.document.inspect import DocumentNotFound
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.render.cli import RenderError
from inkscape_mcp.render.profiles import (
    ProfileResult,
    ProfileSizeError,
)
from inkscape_mcp.render.profiles import (
    create_icon_set as _create_icon_set,
)
from inkscape_mcp.render.profiles import (
    export_print_profile as _export_print_profile,
)
from inkscape_mcp.render.profiles import (
    export_web_profile as _export_web_profile,
)
from inkscape_mcp.server import mcp
from inkscape_mcp.workspace.limits import LimitExceeded
from inkscape_mcp.workspace.paths import SandboxViolation
from inkscape_mcp.workspace.subprocess_exec import ProcessError

_logger = get_logger("tools.profiles")


class ArtifactRef(BaseModel):
    """A single produced artifact in a profile result.

    ONE LOCATION CONTRACT: `path` and `workspace_relative_path` carry the SAME value — the
    file relative to the WORKSPACE ROOT (carries the `.inkscape-mcp/documents/<doc_id>/...` base) —
    so you open it by a single join to the workspace root with no `find`/`stat`. `path` is kept
    only for back-compat and now means exactly the same thing. `width_px`/`height_px` are the TRUE
    on-disk raster dimensions for raster (PNG) outputs and `None` for vector (PDF/SVG).
    `requested_size_px` is set only for icon-set entries (the square size requested); `scale` is set
    only for responsive-web entries produced via `scales=` (the density multiplier, e.g. 2 for the
    2x). `requested_width_px` is set on every responsive-web PNG entry, including the `widths=` form
    that has no density `scale`, so the requested width of an entry is always identifiable.
    """

    path: str
    workspace_relative_path: str
    format: str
    width_px: int | None
    height_px: int | None
    requested_size_px: int | None = None
    scale: int | None = None
    requested_width_px: int | None = None
    #: CONTENT-TRUTH, computed at produce time from the just-written artifact: raster
    #: (PNG) entries carry `opaque_px` (drawn non-transparent pixel count) + `all_blank`; PDF
    #: entries carry `is_vector` (no embedded raster image) + `fonts_outlined` (no embedded font —
    #: true vector when both hold). Each is None for entries it does not apply to / when skipped.
    opaque_px: int | None = None
    all_blank: bool | None = None
    is_vector: bool | None = None
    fonts_outlined: bool | None = None


class ProfileExportResult(BaseModel):
    """Result of a profile tool: the profile token, its ordered artifacts, and applied settings.

    `applied_settings` records the print/profile-specific options applied (auditable) —
    empty for profiles that apply none.
    """

    doc_id: str
    profile: str
    artifacts: list[ArtifactRef]
    applied_settings: dict[str, str]


def _map_failure(exc: Exception) -> ToolError:
    """Map an engine exception to a stable, host-path-free `ToolError`."""
    if isinstance(exc, (KeyError, DocumentNotFound)):
        return ToolError("document id not found")
    if isinstance(exc, ProfileSizeError):
        # Surface the engine's stable, host-path-free message so the cause is DISTINGUISHABLE
        # (PF5): "...must be a positive integer" (<=0) vs "...exceeds the configured pixel
        # cap" (over-cap) — previously both collapsed to one "limit" message.
        return ToolError(str(exc))
    if isinstance(exc, SandboxViolation):
        # Already a SAFE public message (no host path) — e.g. "path rejected: outside workspace".
        return ToolError(str(exc))
    if isinstance(exc, LimitExceeded):
        return ToolError("export exceeds the configured size or dimension limit")
    if isinstance(exc, ProcessError):
        # CAPABILITY-ABSENT: the Inkscape engine could not be launched on this runtime.
        # Name the discovery tool so the agent can inspect support rather than retry blindly.
        return ToolError(
            "render/export failed: the Inkscape engine is unavailable on this runtime; "
            "call list_capabilities to see what this runtime supports"
        )
    if isinstance(exc, RenderError):
        return ToolError("render/export failed")
    return ToolError("render/export failed")


def _to_result(result: ProfileResult) -> ProfileExportResult:
    """Adapt the engine `ProfileResult` into the tool-facing model."""
    return ProfileExportResult(
        doc_id=result.doc_id,
        profile=result.profile,
        artifacts=[
            ArtifactRef(
                path=a.path,
                workspace_relative_path=a.workspace_relative_path,
                format=a.format,
                width_px=a.width_px,
                height_px=a.height_px,
                requested_size_px=a.requested_size_px,
                scale=a.scale,
                requested_width_px=a.requested_width_px,
                opaque_px=a.opaque_px,
                all_blank=a.all_blank,
                is_vector=a.is_vector,
                fonts_outlined=a.fonts_outlined,
            )
            for a in result.artifacts
        ],
        applied_settings=result.applied_settings,
    )


@mcp.tool
def export_web_profile(
    doc_id: str,
    width_px: int = 1024,
    widths: list[int] | None = None,
    scales: list[int] | None = None,
    out_dir: str | None = None,
    name_prefix: str | None = None,
) -> ProfileExportResult:
    """Export a web-oriented asset set: a responsive PNG set plus one plain SVG.

    When to use: producing a web-ready asset bundle. For a print PDF use `export_print_profile`; for
    a square icon set use `create_icon_set`; for a single export use `export_document`.

    Key params: PNG widths resolve as — explicit `widths` (each a PNG); else density `scales`
    applied to `width_px` (e.g. [1,2,3] -> 1x/2x/3x); else `width_px`. Every PNG is pixel-capped
    before Inkscape runs and distinct on disk; responsive entries report their `scale`. `out_dir`
 writes the set into a caller-chosen dir so a `dist/` tree assembles with no
    `Bash cp` — a relative `out_dir` anchors to the workspace ROOT and is sandbox-checked
    (out-of-workspace rejected "path rejected: outside workspace"); `name_prefix` tags each file.

    Return shape: `ProfileExportResult` — `profile`, `applied_settings`, and ordered `artifacts`
    (ascending width, then one plain SVG last); each carries a `workspace_relative_path` plus
    content-truth fields (PNG: `opaque_px`/`all_blank`).

    Example: `export_web_profile(doc_id, scales=[1, 2, 3], out_dir="dist/web")`

    Risk class: low (export to a sandbox-checked dir; no original overwrite).
    """
    try:
        result = _export_web_profile(
            doc_id,
            width_px=width_px,
            widths=widths,
            scales=scales,
            out_dir=out_dir,
            name_prefix=name_prefix,
        )
    except ProfileSizeError as exc:
        _logger.error("export_web_profile rejected", extra={"doc_id": doc_id, "detail": str(exc)})
        raise _map_failure(exc) from exc
    except (
        KeyError,
        DocumentNotFound,
        SandboxViolation,
        LimitExceeded,
        RenderError,
        ProcessError,
    ) as exc:
        _logger.error("export_web_profile failed", extra={"doc_id": doc_id, "detail": str(exc)})
        raise _map_failure(exc) from exc

    log_tool_call(_logger, tool="export_web_profile", doc_id=doc_id, width_px=width_px)
    return _to_result(result)


@mcp.tool
def create_icon_set(
    doc_id: str,
    sizes: list[int] | None = None,
    out_dir: str | None = None,
    name_prefix: str | None = None,
) -> ProfileExportResult:
    """Export a multi-size square PNG icon set from the source document.

    When to use: producing a standard square icon set in one call. For a responsive web bundle use
    `export_web_profile`; for arbitrary batch specs use `export_batch`.

    Key params: `sizes` is the list of square px sizes (defaults to 16, 32, 48, 64, 128, 256). Each
    must be a positive integer no greater than the configured pixel cap; an out-of-range or
    non-positive size is rejected before Inkscape runs and no partial set is written. `out_dir`
 writes the set into a caller-chosen dir — a relative `out_dir` anchors to the
    workspace ROOT and is sandbox-checked (out-of-workspace rejected "path rejected: outside
    workspace"); `name_prefix` tags each file.

    Return shape: `ProfileExportResult` — `profile`, `applied_settings`, and `artifacts` (each
    carries its `requested_size_px`, a `workspace_relative_path`, and content-truth
    `opaque_px`/`all_blank`).

    Example: `create_icon_set(doc_id, sizes=[16, 32, 64], out_dir="dist/icons")`

    Risk class: low (export to a sandbox-checked dir; no original overwrite).
    """
    try:
        result = _create_icon_set(doc_id, sizes=sizes, out_dir=out_dir, name_prefix=name_prefix)
    except ProfileSizeError as exc:
        _logger.error("create_icon_set rejected size", extra={"doc_id": doc_id, "detail": str(exc)})
        raise _map_failure(exc) from exc
    except (
        KeyError,
        DocumentNotFound,
        SandboxViolation,
        LimitExceeded,
        RenderError,
        ProcessError,
    ) as exc:
        _logger.error("create_icon_set failed", extra={"doc_id": doc_id, "detail": str(exc)})
        raise _map_failure(exc) from exc

    log_tool_call(_logger, tool="create_icon_set", doc_id=doc_id, count=len(result.artifacts))
    return _to_result(result)


@mcp.tool
def export_print_profile(
    doc_id: str,
    out_dir: str | None = None,
    name_prefix: str | None = None,
) -> ProfileExportResult:
    """Export a print-oriented PDF (vector, page area) of the whole document.

    When to use: producing a press-safe PDF. For web assets use `export_web_profile`; for a plain
    (non-print) PDF/PNG/SVG use `export_document`.

    Key params: applies real print-specific Inkscape settings (PDF version pinned to 1.4 + text
    outlined to paths) so output is press-safe and ALWAYS differs from a plain PDF export — even for
    text-free docs, since the plain export defaults to PDF 1.5 while this pins 1.4 (header
    `%PDF-1.4`, a deterministic byte difference). `out_dir` writes into a
    caller-chosen dir — a relative `out_dir` anchors to the workspace ROOT and is sandbox-checked
    (out-of-workspace rejected "path rejected: outside workspace"); `name_prefix` tags the file.

    Return shape: `ProfileExportResult` — `profile`, the auditable `applied_settings`, and
    one PDF in `artifacts` with a `workspace_relative_path` plus content-truth `is_vector` /
    `fonts_outlined` (true vector when both hold).

    Example: `export_print_profile(doc_id, out_dir="dist/print")`

    Risk class: low (export to a sandbox-checked dir; no original overwrite).
    """
    try:
        result = _export_print_profile(doc_id, out_dir=out_dir, name_prefix=name_prefix)
    except (
        KeyError,
        DocumentNotFound,
        SandboxViolation,
        LimitExceeded,
        RenderError,
        ProcessError,
    ) as exc:
        _logger.error("export_print_profile failed", extra={"doc_id": doc_id, "detail": str(exc)})
        raise _map_failure(exc) from exc

    log_tool_call(_logger, tool="export_print_profile", doc_id=doc_id)
    return _to_result(result)
