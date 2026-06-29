"""Export tools: `render_preview`, `capture_frame`, `list_frames`, `export_document`,
`export_object`.

Thin MCP layer over the CLI render/export engine (`inkscape_mcp.render.cli`). Inkscape engine
per ADR-005. All are risk class **low**: they write only to the per-document artifact / exports
dir and never overwrite the original or the working copy, so no Operation Record or
snapshot is required.

Client-facing errors are raised as `ToolError` with stable, host-path-free messages (sec.12):
unknown document id -> "document id not found"; bad / missing object id -> a stable message;
limit / process failures -> stable messages. Returned artifact paths follow ONE LOCATION CONTRACT
: `artifact_path` and `workspace_relative_path` carry the SAME value, always relative to the
WORKSPACE ROOT, openable by a single join to the root — never an absolute host path.
"""

from __future__ import annotations

import json
from pathlib import Path

import mcp.types as mcp_types
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image
from pydantic import BaseModel

from inkscape_mcp.config import get_settings
from inkscape_mcp.document.inspect import DocumentNotFound, InspectionError, inspect_objects
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.registry import DocEntry, get_registry
from inkscape_mcp.render.cli import (
    InvalidObjectId,
    RenderError,
    is_safe_object_id,
)
from inkscape_mcp.render.cli import (
    capture_frame as _capture_frame,
)
from inkscape_mcp.render.cli import (
    export_document as _export_document,
)
from inkscape_mcp.render.cli import (
    export_object as _export_object,
)
from inkscape_mcp.render.cli import (
    list_frames as _list_frames,
)
from inkscape_mcp.render.cli import (
    render_preview as _render_preview,
)
from inkscape_mcp.server import mcp
from inkscape_mcp.workspace.limits import LimitExceeded
from inkscape_mcp.workspace.paths import SandboxViolation
from inkscape_mcp.workspace.subprocess_exec import ProcessError

_logger = get_logger("tools.export")

#: Whole-document export formats accepted by `export_document`.
_DOCUMENT_FORMATS = frozenset({"png", "pdf", "svg"})
#: Single-object export formats accepted by `export_object`.
_OBJECT_FORMATS = frozenset({"png", "pdf", "svg"})

#: DEFAULT inline-image byte threshold. A produced raster is returned INLINE as an MCP
#: ImageContent block only when it is at or below this many bytes, so the agent sees its output
#: without a second `Read` of the artifact path. The default is deliberately small (≈5 MiB) — far
#: below the hard `Settings.max_output_bytes` artifact cap — because an inlined PNG is
#: base64-encoded into the MCP message; an over-threshold export still produces the file and
#: reports its path, it is just not embedded. A caller can raise/lower this per call via
#: `max_output_bytes`, or set `inline=False` to opt out of embedding entirely.
DEFAULT_INLINE_MAX_BYTES = 5 * 1024 * 1024

#: Raster formats whose artifact bytes can be embedded as an MCP ImageContent block. Vector outputs
#: (PDF/SVG) are never inlined — they are not an image content type — so they always return the bare
#: structured result with the artifact path.
_INLINE_FORMATS = frozenset({"png"})


def _resolve_artifact_abspath(doc_id: str, workspace_relative_path: str) -> Path | None:
    """Resolve a SERVER-MINTED workspace-relative artifact path to its absolute on-disk path.

    sec.12: the path is the registry-owned `entry.root` joined to the value the render/export engine
    just produced (never client input), so the resolved file is always inside the sandbox. Returns
    None if the registry entry is gone or the file is missing (the caller then skips inlining rather
    than failing the whole call).
    """
    try:
        entry: DocEntry = get_registry().get(doc_id)
    except KeyError:  # pragma: no cover - the export just succeeded for this id
        return None
    candidate = Path(entry.root) / workspace_relative_path
    return candidate if candidate.is_file() else None


def _inline_image(
    *,
    doc_id: str,
    workspace_relative_path: str,
    fmt: str,
    inline: bool,
    max_output_bytes: int | None,
) -> Image | None:
    """Build an MCP `Image` for a produced raster artifact, gated by `inline` + the byte threshold.

    Returns None (skip embedding) when: `inline` is False; the format is not an inlineable raster
    (PDF/SVG); the artifact cannot be resolved/read; or its on-disk size exceeds the effective
    threshold (`max_output_bytes` when given and > 0, else `DEFAULT_INLINE_MAX_BYTES`). The bytes
    are read from the SERVER-MINTED sandbox artifact path, never from client input (sec.12).
    """
    if not inline or fmt not in _INLINE_FORMATS:
        return None
    threshold = (
        max_output_bytes
        if (max_output_bytes is not None and max_output_bytes > 0)
        else (DEFAULT_INLINE_MAX_BYTES)
    )
    abspath = _resolve_artifact_abspath(doc_id, workspace_relative_path)
    if abspath is None:
        return None
    try:
        size = abspath.stat().st_size
    except OSError:  # pragma: no cover - file existence just checked
        return None
    if size > threshold:
        _logger.info(
            "inline image skipped: over threshold",
            extra={"doc_id": doc_id, "size": size, "threshold": threshold},
        )
        return None
    # Defense in depth: never embed a file larger than the configured hard artifact cap either.
    if size > get_settings().max_output_bytes:
        return None
    try:
        data = abspath.read_bytes()
    except OSError:  # pragma: no cover
        return None
    return Image(data=data, format=fmt)


#: Local-only artifact path fields scrubbed from the human/agent-facing TEXT payload when the
#: raster is attached inline: they are workspace-root-relative (sec.12, never absolute), so a client
#: that tries to `Read` them resolves against its own CWD (!= the server workspace root) and fails.
#: The full values stay in `structured_content` for machine consumers and schema validation.
_INLINE_SCRUBBED_PATH_FIELDS = ("artifact_path", "workspace_relative_path")

#: Appended to the inline text payload so the agent trusts the attached image instead of chasing a
#: path it cannot resolve.
_INLINE_NOTE = (
    "Raster attached inline as an image — view it directly. The artifact is stored server-side "
    "at a workspace-relative path (see structured output); it is NOT on your local filesystem, "
    "so do not Read it."
)


def _with_inline[ModelT: BaseModel](result: ModelT, image: Image | None) -> ModelT | ToolResult:
    """Return the bare structured `result`, or a `ToolResult` carrying it PLUS the inline image.

    When `image` is None the bare Pydantic model is returned unchanged (existing direct callers and
    structured-output consumers see exactly the same shape as before — is additive). When an
    image is present the inline raster is appended as an MCP ImageContent block (so the agent
    perceives its output without a second `Read`), the full structured result is preserved in
    `structured_content` (so `.artifact_path` / dims / `stale` remain machine-readable), and the
    human-facing TEXT payload is built WITHOUT the workspace-relative path fields — replaced by a
    short note — so a path-chasing agent is not lured into a `Read` of a server-side path it cannot
    resolve (1B).
    """
    if image is None:
        return result
    structured = result.model_dump()
    display = {k: v for k, v in structured.items() if k not in _INLINE_SCRUBBED_PATH_FIELDS}
    display["note"] = _INLINE_NOTE
    return ToolResult(
        content=[
            mcp_types.TextContent(type="text", text=json.dumps(display)),
            image.to_image_content(),
        ],
        structured_content=structured,
    )


class PreviewResult(BaseModel):
    """Result of `render_preview`: a resolvable PNG path plus its TRUE raster size.

    ONE LOCATION CONTRACT: `artifact_path` and `workspace_relative_path` carry the SAME
    value — the file relative to the WORKSPACE ROOT (carries the
    `.inkscape-mcp/documents/<doc_id>/...` base) — so you open it by a single join to the workspace
    root with no `find`/`stat`. `artifact_path` is kept only for back-compat and now means exactly
    the same thing. `width_px`/`height_px` are the on-disk raster dimensions.
    """

    doc_id: str
    artifact_path: str
    workspace_relative_path: str
    format: str
    width_px: int
    height_px: int
    #: STALENESS SIGNAL: True iff the working copy changed after this preview was made, so
    #: it no longer reflects the current document. Computed at produce time (artifact mtime vs.
    #: working-copy mtime) by `inkscape_mcp.render.cli.compute_stale`; a freshly rendered preview
    #: reflects the current working copy, so this is False.
    stale: bool = False
    #: CONTENT-TRUTH: `opaque_px` is the count of drawn (non-transparent) pixels in the PNG
    #: and `all_blank` is True iff nothing was drawn, so a caller can prove the render is not blank
    #: straight from the result. None if verification was skipped/failed.
    opaque_px: int | None = None
    all_blank: bool | None = None


class FrameResult(BaseModel):
    """Result of `capture_frame`: a `render_preview`-style PNG plus its series + index.

    `artifact_path` / `workspace_relative_path` carry the SAME root-relative value, opened
    by a single join to the workspace root. `series` is the (sanitized) series folder under
    `artifacts/frames/`; `frame_index` is the 1-based position within that series.
    `width_px`/`height_px` are the on-disk raster dimensions.
    """

    doc_id: str
    artifact_path: str
    workspace_relative_path: str
    format: str
    width_px: int
    height_px: int
    series: str
    frame_index: int
    #: STALENESS SIGNAL: True iff the working copy changed after this frame was captured.
    #: Computed at produce time (artifact mtime vs. working-copy mtime); a freshly captured frame
    #: reflects the current working copy, so this is False.
    stale: bool = False


class FrameInfo(BaseModel):
    """One frame in a series listing: its index and resolvable workspace path."""

    frame_index: int
    artifact_path: str
    workspace_relative_path: str


class FrameListResult(BaseModel):
    """Result of `list_frames`: a series' frames ordered by index (empty if the series is unused).

    Each entry is resolvable by a single join to the workspace root.
    """

    doc_id: str
    series: str
    frames: list[FrameInfo]


class ExportResult(BaseModel):
    """Result of `export_document` / `export_object`.

    ONE LOCATION CONTRACT: `artifact_path` and `workspace_relative_path` carry the SAME
    value — the file relative to the WORKSPACE ROOT (a managed output carries the
    `.inkscape-mcp/documents/<doc_id>/...` base; an `out_dir` output is its in-workspace relative
    path) — so you open it by a single join to the workspace root with no `find`/`stat`, for EVERY
    output. `artifact_path` is kept only for back-compat and now means exactly the same thing.
    `width_px`/`height_px` are the TRUE on-disk raster dimensions for raster (PNG) exports
    and `None` for vector (PDF/SVG) outputs.
    """

    doc_id: str
    artifact_path: str
    workspace_relative_path: str
    format: str
    width_px: int | None
    height_px: int | None
    #: STALENESS SIGNAL: True iff the working copy changed after this export was produced.
    #: Computed at produce time (artifact mtime vs. working-copy mtime); a freshly produced export
    #: reflects the current working copy, so this is False.
    stale: bool = False
    #: CONTENT-TRUTH, computed at produce time from the just-written artifact:
    #: raster (PNG) outputs carry `opaque_px` (drawn non-transparent pixel count) + `all_blank`
    #: (True iff nothing was drawn); PDF outputs carry `is_vector` (no embedded raster image) +
    #: `fonts_outlined` (no embedded font — text outlined to paths; true vector when both hold).
    #: Each field is None for outputs it does not apply to, or when verification was skipped/failed.
    opaque_px: int | None = None
    all_blank: bool | None = None
    is_vector: bool | None = None
    fonts_outlined: bool | None = None


def _model_output_schema(model: type[BaseModel]) -> dict[str, object]:
    """Derive the `outputSchema` FastMCP would emit for a bare ``model`` return.

    The inline-image tools (`render_preview` / `capture_frame` / `export_document` /
    `export_object`) annotate their return as ``<Model> | ToolResult`` so they can optionally append
    an MCP image block. FastMCP cannot auto-derive an `outputSchema` from that union (the
    `ToolResult` arm is unserializable), so the wire tool would carry a NULL schema and the client
    loses structured-output validation. We pass this schema EXPLICITLY to `@mcp.tool(...)`; it is
    byte-for-byte what FastMCP derives for the bare ``model`` (same `TypeAdapter` serialization +
    `compress_schema(prune_titles=True)` it uses internally), so the structured content the tools
    already return — the model's `model_dump()` — validates against it unchanged. No DATA change.
    """
    from fastmcp.utilities.json_schema import compress_schema
    from pydantic import TypeAdapter

    schema = TypeAdapter(model).json_schema(mode="serialization")
    return compress_schema(schema, prune_titles=True)


def _map_failure(exc: Exception) -> ToolError:
    """Map an engine exception to a stable, host-path-free `ToolError`."""
    if isinstance(exc, (KeyError, DocumentNotFound)):
        return ToolError("document id not found")
    if isinstance(exc, SandboxViolation):
        # Already a SAFE public message (no host path) — e.g. "path rejected: outside workspace".
        return ToolError(str(exc))
    if isinstance(exc, LimitExceeded):
        return ToolError("export exceeds the configured size or dimension limit")
    if isinstance(exc, InvalidObjectId):
        return ToolError("object id is not valid")
    if isinstance(exc, ProcessError):
        # CAPABILITY-ABSENT: the Inkscape engine could not be launched (e.g. no binary on
        # this runtime). Name the discovery tool so the agent can see what this runtime supports
        # rather than retrying blindly. Stable + host-path-free (sec.12).
        return ToolError(
            "render/export failed: the Inkscape engine is unavailable on this runtime; "
            "call list_capabilities to see what this runtime supports"
        )
    if isinstance(exc, RenderError):
        return ToolError("render/export failed")
    return ToolError("render/export failed")


@mcp.tool(output_schema=_model_output_schema(PreviewResult))
def render_preview(
    doc_id: str,
    width_px: int | None = None,
    name: str | None = None,
    inline: bool = True,
    max_output_bytes: int | None = None,
) -> PreviewResult | ToolResult:
    """Render a PNG preview of the whole document into the artifacts dir.

        When to use: a quick visual check of the whole document. For a final file use `export_document`;
        for one object use `export_object`; for an ordered run series use `capture_frame`.

        Key params: `width_px` scales the raster (height follows the document aspect ratio); omit for
        intrinsic size. Oversized requests are rejected before Inkscape runs. `name` tags the file
        (successive calls do NOT clobber, — each render gets a unique frame name). INLINE RASTER
    : by default the PNG is also returned as an MCP image block so the agent SEES it without
        a second `Read` — do NOT `Read` the returned path (it is server-side, not on your
        filesystem); gated by `max_output_bytes` (~5 MiB default) and skipped for an oversized
        render; `inline=False` returns only the structured result.

        Return shape: `PreviewResult` — `artifact_path` / `workspace_relative_path` (same root-relative
        value), `format`, `width_px`/`height_px` (TRUE on-disk size), `stale`. With an inline
        image, a `ToolResult` carrying the same structured fields plus the image block.

        Example: `render_preview(doc_id, width_px=512)`

        Risk class: low (render/export to artifact dir; no original overwrite).
    """
    try:
        result = _render_preview(doc_id, width_px=width_px, name=name)
    except (KeyError, DocumentNotFound, LimitExceeded, RenderError, ProcessError) as exc:
        _logger.error("render_preview failed", extra={"doc_id": doc_id, "detail": str(exc)})
        raise _map_failure(exc) from exc

    log_tool_call(_logger, tool="render_preview", doc_id=doc_id)
    preview = PreviewResult(
        doc_id=result.doc_id,
        artifact_path=result.artifact_path,
        workspace_relative_path=result.workspace_relative_path,
        format=result.format,
        width_px=result.width_px if result.width_px is not None else 0,
        height_px=result.height_px if result.height_px is not None else 0,
        stale=result.stale,
        opaque_px=result.opaque_px,
        all_blank=result.all_blank,
    )
    image = _inline_image(
        doc_id=doc_id,
        workspace_relative_path=preview.workspace_relative_path,
        fmt=preview.format,
        inline=inline,
        max_output_bytes=max_output_bytes,
    )
    return _with_inline(preview, image)


@mcp.tool(output_schema=_model_output_schema(FrameResult))
def capture_frame(
    doc_id: str,
    series: str | None = None,
    width_px: int | None = None,
    label: str | None = None,
    inline: bool = True,
    max_output_bytes: int | None = None,
) -> FrameResult | ToolResult:
    """Capture the next numbered PNG screenshot in a per-run frame series.

        When to use: documenting a scripted edit sequence step-by-step. For a one-off check use
        `render_preview`; to gather a finished series use `list_frames`.

        Key params: `series` (sanitized; defaults to `run`) groups frames into a folder under
        `artifacts/frames/<series>/`; the index is derived from the filesystem (highest existing
        `frame-NNN` + 1) — monotonic, survives a restart, never clobbers. `label` is folded into the
        frame name. Renders the whole canvas exactly like `render_preview` (no UI chrome). INLINE RASTER
    : the PNG is returned inline by default (gated by `max_output_bytes`); `inline=False`
        returns only the structured result. Do NOT `Read` the returned path — view the inline image
        (the path is server-side, workspace-relative, not on your filesystem).

        Return shape: `FrameResult` — `artifact_path` / `workspace_relative_path` (same value),
        `format`, `width_px`/`height_px`, `series`, `frame_index` (1-based), `stale`. With an inline
        image, a `ToolResult` carrying the same fields plus the image block.

        Example: `capture_frame(doc_id, series="cleanup", label="after-simplify")`

        Risk class: low (render to the managed artifacts dir; no original overwrite, no Operation
        Record).
    """
    try:
        result = _capture_frame(doc_id, series=series, width_px=width_px, label=label)
    except (KeyError, DocumentNotFound, LimitExceeded, RenderError, ProcessError) as exc:
        _logger.error("capture_frame failed", extra={"doc_id": doc_id, "detail": str(exc)})
        raise _map_failure(exc) from exc

    log_tool_call(_logger, tool="capture_frame", doc_id=doc_id, series=result.series)
    frame = FrameResult(
        doc_id=result.doc_id,
        artifact_path=result.artifact_path,
        workspace_relative_path=result.workspace_relative_path,
        format=result.format,
        width_px=result.width_px if result.width_px is not None else 0,
        height_px=result.height_px if result.height_px is not None else 0,
        series=result.series,
        frame_index=result.frame_index,
        stale=result.stale,
    )
    image = _inline_image(
        doc_id=doc_id,
        workspace_relative_path=frame.workspace_relative_path,
        fmt=frame.format,
        inline=inline,
        max_output_bytes=max_output_bytes,
    )
    return _with_inline(frame, image)


@mcp.tool
def list_frames(doc_id: str, series: str | None = None) -> FrameListResult:
    """List the frames of a `capture_frame` series, ordered by index.

    When to use: gathering a whole run's PNGs at the end without re-deriving paths. To produce
    frames use `capture_frame`.

    Key params: `series` is sanitized identically to `capture_frame` (defaults to `run`).

    Return shape: `FrameListResult` — `doc_id`, `series`, and `frames` (each a `FrameInfo` with
    `frame_index` + a resolvable `workspace_relative_path`), ordered by index; empty when the series
    has no frames yet.

    Example: `list_frames(doc_id, series="cleanup")`

    Risk class: low (read-only listing of the managed artifacts dir).
    """
    try:
        listing = _list_frames(doc_id, series=series)
    except (KeyError, DocumentNotFound) as exc:
        raise _map_failure(exc) from exc

    log_tool_call(_logger, tool="list_frames", doc_id=doc_id, count=len(listing.frames))
    return FrameListResult(
        doc_id=doc_id,
        series=listing.series,
        frames=[
            FrameInfo(
                frame_index=ref.frame_index,
                artifact_path=ref.artifact_path,
                workspace_relative_path=ref.workspace_relative_path,
            )
            for ref in listing.frames
        ],
    )


@mcp.tool(output_schema=_model_output_schema(ExportResult))
def export_document(
    doc_id: str,
    format: str,
    width_px: int | None = None,
    out_dir: str | None = None,
    name_prefix: str | None = None,
    inline: bool = True,
    max_output_bytes: int | None = None,
) -> ExportResult | ToolResult:
    """Export the whole document to PNG, PDF, or SVG.

    When to use: producing a final file of the whole document. For one object use `export_object`;
    for many sizes/formats at once use `export_batch`; for a web/print bundle use the profile tools.

    Key params: `format` is one of "png"/"pdf"/"svg" (others rejected). PNG honors `width_px`
    (pixel-capped before Inkscape runs); PDF/SVG are vector and ignore it. `out_dir` writes
    into a caller-chosen dir — a relative `out_dir` anchors to the workspace ROOT and is
    sandbox-checked (out-of-workspace is rejected with "path rejected: outside workspace");
    `name_prefix` tags the filename. INLINE RASTER: a PNG is returned inline by default
    (gated by `max_output_bytes`); PDF/SVG are never embedded; `inline=False` opts out. For an
    inlined PNG, view the image — do NOT `Read` the returned path (server-side, workspace-relative,
    not on your filesystem).

    Return shape: `ExportResult` — `artifact_path` / `workspace_relative_path` (same value),
    `format`, `width_px`/`height_px` (TRUE size for PNG, None for vector), `stale`. With an inline
    image, a `ToolResult` carrying the same fields plus the image block.

    Example: `export_document(doc_id, "png", width_px=1024)`

    Risk class: low (render/export to a sandbox-checked dir; no original overwrite).
    """
    fmt = format.lower().strip()
    if fmt not in _DOCUMENT_FORMATS:
        raise ToolError(
            "unsupported export format (expected one of: png, pdf, svg); "
            "call list_capabilities to see the supported export formats"
        )
    try:
        result = _export_document(
            doc_id, fmt, width_px=width_px, out_dir=out_dir, name_prefix=name_prefix
        )
    except (
        KeyError,
        DocumentNotFound,
        SandboxViolation,
        LimitExceeded,
        RenderError,
        ProcessError,
    ) as exc:
        _logger.error("export_document failed", extra={"doc_id": doc_id, "detail": str(exc)})
        raise _map_failure(exc) from exc

    log_tool_call(_logger, tool="export_document", doc_id=doc_id, format=fmt)
    export = ExportResult(
        doc_id=result.doc_id,
        artifact_path=result.artifact_path,
        workspace_relative_path=result.workspace_relative_path,
        format=result.format,
        width_px=result.width_px,
        height_px=result.height_px,
        stale=result.stale,
        opaque_px=result.opaque_px,
        all_blank=result.all_blank,
        is_vector=result.is_vector,
        fonts_outlined=result.fonts_outlined,
    )
    image = _inline_image(
        doc_id=doc_id,
        workspace_relative_path=export.workspace_relative_path,
        fmt=export.format,
        inline=inline,
        max_output_bytes=max_output_bytes,
    )
    return _with_inline(export, image)


@mcp.tool(output_schema=_model_output_schema(ExportResult))
def export_object(
    doc_id: str,
    object_id: str,
    format: str = "png",
    width_px: int | None = None,
    out_dir: str | None = None,
    name_prefix: str | None = None,
    inline: bool = True,
    max_output_bytes: int | None = None,
) -> ExportResult | ToolResult:
    """Export a single object (by id) to PNG, PDF, or SVG.

    When to use: exporting one object, clipped to its own bbox (get the id from `find_objects`). For
    the whole document use `export_document`; for many at once use `export_batch`.

    Key params: `object_id` must exist and match the safe SVG-id charset (else rejected before it
    reaches Inkscape). `format` is one of "png"/"pdf"/"svg". `out_dir` writes into a
    caller-chosen dir — a relative `out_dir` anchors to the workspace ROOT and is sandbox-checked
    (out-of-workspace is rejected with "path rejected: outside workspace"); `name_prefix` tags the
    filename. INLINE RASTER: a PNG is returned inline by default (gated by
    `max_output_bytes`); PDF/SVG never embedded; `inline=False` opts out. For an inlined PNG, view
    the image — do NOT `Read` the returned path (server-side, workspace-relative, not on your
    filesystem).

    Return shape: `ExportResult` — `artifact_path` / `workspace_relative_path` (same value),
    `format`, `width_px`/`height_px` (TRUE size for PNG, None for vector), `stale`. With an inline
    image, a `ToolResult` carrying the same fields plus the image block.

    Example: `export_object(doc_id, "logo", "svg")`

    Risk class: low (render/export to a sandbox-checked dir; no original overwrite).
    """
    fmt = format.lower().strip()
    if fmt not in _OBJECT_FORMATS:
        raise ToolError(
            "unsupported export format (expected one of: png, pdf, svg); "
            "call list_capabilities to see the supported export formats"
        )

    # Validate the id BEFORE it can reach argv: safe charset + actually present in the document.
    if not is_safe_object_id(object_id):
        raise ToolError("object id is not valid")
    try:
        objects = inspect_objects(doc_id)
    except DocumentNotFound as exc:
        raise ToolError("document id not found") from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc
    if not any(obj.id == object_id for obj in objects.objects):
        raise ToolError("object id not found in document")

    try:
        result = _export_object(
            doc_id, object_id, fmt, width_px=width_px, out_dir=out_dir, name_prefix=name_prefix
        )
    except (
        KeyError,
        DocumentNotFound,
        SandboxViolation,
        LimitExceeded,
        RenderError,
        ProcessError,
        InvalidObjectId,
    ) as exc:
        _logger.error("export_object failed", extra={"doc_id": doc_id, "detail": str(exc)})
        raise _map_failure(exc) from exc

    log_tool_call(_logger, tool="export_object", doc_id=doc_id, object_id=object_id, format=fmt)
    export = ExportResult(
        doc_id=result.doc_id,
        artifact_path=result.artifact_path,
        workspace_relative_path=result.workspace_relative_path,
        format=result.format,
        width_px=result.width_px,
        height_px=result.height_px,
        stale=result.stale,
        opaque_px=result.opaque_px,
        all_blank=result.all_blank,
        is_vector=result.is_vector,
        fonts_outlined=result.fonts_outlined,
    )
    image = _inline_image(
        doc_id=doc_id,
        workspace_relative_path=export.workspace_relative_path,
        fmt=export.format,
        inline=inline,
        max_output_bytes=max_output_bytes,
    )
    return _with_inline(export, image)
