"""Focused visual before/after diff (ADR-006, low risk).

The decisive "better than inkmcp" feedback step: instead of dumping two raw whole-window
screenshots, ``live_diff_view`` resolves the before/after frames a live mutation ALREADY captured
(``run_live_mutation`` persists ``preview_before`` / ``preview_after`` on each
``LiveOperationRecord``), computes the CHANGED-REGION bbox by pixel-diffing the two frames
(``PIL.ImageChops.difference(...).getbbox()``), and renders ONE annotated overlay that highlights
the changed bbox, the selection outline (scene bboxes from), and the highlighted ids.

ARTIFACT-ONLY / LOW RISK: this never mutates the live document, never opens or routes through
``run_live_mutation`` / ``apply_edit``, and needs no approval — it reads two PNGs, diffs them, and
writes one PNG under the live artifacts dir (mirrors ``render_live_view`` / ``export_selection``).

SAFETY (sec.12): the before/after frames are resolved VIA THE OPERATION ID (the record's own
workspace-relative preview paths), never a raw client-supplied path; each resolved frame is
sandbox-validated to stay under the live artifacts dir before any bytes are read. The output
artifact gets a server-minted, operation-id-based name, lands only under the live artifacts dir, is
bounded by the output-size cap, and only a workspace-relative path is returned. Frames must share
dimensions to diff; a mismatch is a stable, host-path-free error. No network.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw
from pydantic import BaseModel, Field

from inkscape_mcp.config import Settings, get_settings
from inkscape_mcp.live.records import (
    _OPERATION_ID_RE,
    LiveOperationRecord,
    get_live_operation,
    update_live_operation,
)
from inkscape_mcp.live.transport import BBox, LiveError, SceneSelectionItem
from inkscape_mcp.logging_setup import get_logger, log_preview
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.limits import LimitExceeded, check_input_size, check_output_size

_logger = get_logger("live.diff")

#: Colours for the annotation overlay (RGBA). Changed-region bbox in red, selection outline in cyan.
_CHANGED_OUTLINE = (255, 0, 0, 255)
_SELECTION_OUTLINE = (0, 200, 255, 255)
_OUTLINE_WIDTH = 2


class LiveDiffError(LiveError):
    """A focused visual diff could not be produced (e.g. missing frames or mismatched sizes)."""


class LiveDiffResult(BaseModel):
    """Outcome of `live_diff_view`: the annotated overlay + the computed changed-region bbox.

    `operation_id` links the diff back to its Live Operation Record. `artifact_path` is the
    WORKSPACE-RELATIVE annotated overlay PNG. `changed_bbox` is the pixel-space bounding box of the
    differing region (`null` when the before/after frames are identical — no change to highlight).
    """

    operation_id: str = Field(description="Live Operation Record id this diff was computed for.")
    artifact_path: str = Field(description="Workspace-relative annotated overlay PNG path.")
    changed_bbox: BBox | None = Field(
        default=None, description="Changed-region bbox in PIXELS (null when frames are identical)."
    )
    width: int = Field(description="Frame width in pixels.")
    height: int = Field(description="Frame height in pixels.")
    highlighted_ids: list[str] = Field(
        default_factory=list, description="Selection ids whose outline was drawn on the overlay."
    )


def _first_root(settings: Settings) -> Path:
    if not settings.workspace_roots:
        raise LiveDiffError("no workspace root configured to store the live diff")
    return settings.workspace_roots[0]


def _resolve_frame(root: Path, rel: str) -> Path:
    """Resolve a record's workspace-relative preview path, sandbox-validated under live artifacts.

    The path comes from the server-minted record (not a raw client arg), but is still containment-
    checked: it must canonicalize to a real file UNDER the live artifacts dir. Raises
    `LiveDiffError` (host-path-free) otherwise.
    """
    artifacts = sandbox.live_artifacts_dir(root)
    try:
        resolved = (root / rel).resolve(strict=True)
        artifacts_real = artifacts.resolve(strict=True)
    except OSError:
        raise LiveDiffError("a before/after frame for this operation is unavailable") from None
    if not resolved.is_relative_to(artifacts_real) or not resolved.is_file():
        raise LiveDiffError("a before/after frame for this operation is unavailable")
    return resolved


def _selection_bboxes(scene_selection: list[SceneSelectionItem]) -> list[tuple[str, BBox]]:
    """Selection items that carry a bbox, as (id, bbox) pairs (skips bbox-less entries)."""
    return [(item.id, item.bbox) for item in scene_selection if item.bbox is not None]


def _map_to_pixels(
    bbox: BBox, canvas: BBox | None, img_w: int, img_h: int
) -> tuple[int, int, int, int] | None:
    """Map a user-unit selection bbox to a pixel rect using the canvas→image scale, or None.

    Returns None when the canvas size is unknown/degenerate (so the overlay simply omits the
    selection outline rather than drawing a wrong rectangle). Clamps to the image bounds.
    """
    if canvas is None or canvas.width <= 0 or canvas.height <= 0:
        return None
    sx = img_w / canvas.width
    sy = img_h / canvas.height
    x0 = round((bbox.x - canvas.x) * sx)
    y0 = round((bbox.y - canvas.y) * sy)
    x1 = round((bbox.x - canvas.x + bbox.width) * sx)
    y1 = round((bbox.y - canvas.y + bbox.height) * sy)
    x0, x1 = max(0, min(x0, x1)), min(img_w, max(x0, x1))
    y0, y1 = max(0, min(y0, y1)), min(img_h, max(y0, y1))
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def compute_changed_bbox(before: Image.Image, after: Image.Image) -> BBox | None:
    """Pixel-diff two same-size frames and return the changed-region bbox, or None if identical.

    Uses the canonical `ImageChops.difference(...).getbbox()`: `getbbox` returns the bounding box of
    all non-zero (differing) pixels, or `None` when the frames are identical. Raises `LiveDiffError`
    on a size mismatch (the two frames must share dimensions to diff). Both inputs are normalized to
    RGB so the diff ignores alpha-channel noise.
    """
    if before.size != after.size:
        raise LiveDiffError("before/after frames have different dimensions and cannot be diffed")
    box = ImageChops.difference(before.convert("RGB"), after.convert("RGB")).getbbox()
    if box is None:
        return None
    left, upper, right, lower = box
    return BBox(
        x=float(left),
        y=float(upper),
        width=float(right - left),
        height=float(lower - upper),
    )


def _annotate(
    after: Image.Image,
    changed: BBox | None,
    selection_rects: list[tuple[str, tuple[int, int, int, int]]],
) -> Image.Image:
    """Draw the changed-region bbox + selection outlines onto a copy of the after-frame."""
    overlay = after.convert("RGBA").copy()
    draw = ImageDraw.Draw(overlay)
    for _oid, (x0, y0, x1, y1) in selection_rects:
        draw.rectangle((x0, y0, x1, y1), outline=_SELECTION_OUTLINE, width=_OUTLINE_WIDTH)
    if changed is not None:
        cx0 = round(changed.x)
        cy0 = round(changed.y)
        cx1 = round(changed.x + changed.width)
        cy1 = round(changed.y + changed.height)
        draw.rectangle((cx0, cy0, cx1, cy1), outline=_CHANGED_OUTLINE, width=_OUTLINE_WIDTH)
    return overlay


def diff_live_operation(
    operation_id: str,
    *,
    selection: list[SceneSelectionItem] | None = None,
    canvas: BBox | None = None,
    settings: Settings | None = None,
) -> LiveDiffResult:
    """Produce a focused, annotated before/after visual diff for one Live Operation Record.

    Resolves the before/after frames the mutation ALREADY captured (`preview_before` /
    `preview_after` on the record — reuse, not re-derivation), pixel-diffs them to a changed-region
    bbox, and writes ONE annotated overlay (changed bbox + selection outlines) under the live
    artifacts dir with a server-minted, operation-id-based name. Returns a workspace-relative path,
    the operation id, and the pixel-space changed bbox. The diff path is appended to the record's
    `diff_artifacts` so it is linkable to the operation. Artifact-only — no document mutation, no
    Operation Record routing, no approval.
    """
    s = settings if settings is not None else get_settings()
    # Validate the id shape up front so the output filename (live-diff-<id>.png) is independently
    # safe — never coupled to the record lookup raising first (defence in depth, no traversal).
    if not _OPERATION_ID_RE.match(operation_id):
        raise LiveDiffError("no live operation with that id")
    root = _first_root(s).resolve()
    try:
        record: LiveOperationRecord = get_live_operation(operation_id, settings=s)
    except KeyError:
        raise LiveDiffError("no live operation with that id") from None

    before_rel = record.previews.get("before")
    after_rel = record.previews.get("after")
    if not before_rel or not after_rel:
        raise LiveDiffError("this operation has no before/after frames to diff")

    before_path = _resolve_frame(root, before_rel)
    after_path = _resolve_frame(root, after_rel)

    # Bound BOTH the on-disk frame bytes (file cap) and the decompressed pixel count (bomb guard)
    # before PIL decodes anything into memory.
    try:
        check_input_size(before_path, s)
        check_input_size(after_path, s)
    except LimitExceeded as exc:
        raise LiveDiffError("a before/after frame is too large to diff") from exc
    Image.MAX_IMAGE_PIXELS = max(1, s.max_export_px) ** 2

    try:
        with Image.open(before_path) as before_img, Image.open(after_path) as after_img:
            before_img.load()
            after_img.load()
            changed = compute_changed_bbox(before_img, after_img)
            img_w, img_h = after_img.size

            sel_items = selection if selection is not None else []
            selection_rects: list[tuple[str, tuple[int, int, int, int]]] = []
            for oid, bbox in _selection_bboxes(sel_items):
                rect = _map_to_pixels(bbox, canvas, img_w, img_h)
                if rect is not None:
                    selection_rects.append((oid, rect))
            overlay = _annotate(after_img, changed, selection_rects)
    except Image.DecompressionBombError as exc:
        raise LiveDiffError("a before/after frame exceeds the pixel limit") from exc

    sandbox.ensure_live_dirs(root)
    out = sandbox.live_artifacts_dir(root) / f"live-diff-{operation_id}.png"
    tmp = out.with_name(f"{out.name}.tmp")
    try:
        overlay.save(tmp, format="PNG")
        tmp.replace(out)
    finally:
        # If save or replace failed, never leave an orphaned .tmp behind.
        tmp.unlink(missing_ok=True)

    try:
        check_output_size(out, s)
    except Exception:
        out.unlink(missing_ok=True)
        raise

    rel = out.relative_to(root).as_posix()
    highlighted = [oid for oid, _rect in selection_rects]
    # Link the diff back to the operation (append-only; never replaces the source previews).
    try:
        update_live_operation(
            record,
            settings=s,
            diff_artifacts=[*record.diff_artifacts, rel],
        )
    except Exception:  # pragma: no cover - linking is best-effort, never blocks the diff artifact
        _logger.warning("could not link diff artifact to record", extra={"event": "diff"})

    log_preview(_logger, doc_id=None, format="png", artifact=rel, live=True)
    return LiveDiffResult(
        operation_id=operation_id,
        artifact_path=rel,
        changed_bbox=changed,
        width=img_w,
        height=img_h,
        highlighted_ids=highlighted,
    )
