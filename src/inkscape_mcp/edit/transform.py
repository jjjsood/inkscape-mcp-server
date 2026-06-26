"""Direct-DOM transform engine (ADR-005).

Pure ``mutate(tree) -> str`` functions used as the callbacks for the shared edit pipeline
(:func:`inkscape_mcp.edit.pipeline.apply_edit`). Each one edits the parsed working tree IN
MEMORY and returns a short human summary of what changed; none of them carry an ``@mcp.tool``
decorator, perform any I/O, or invoke Inkscape — they are the per-tool kernels behind the thin
MCP layer in ``inkscape_mcp.tools.transform``.

Scope (ADR-005): SIMPLE transforms only. Object moves/scales/rotations are expressed as SVG
``transform`` functions prepended to the target element (so they compose in parent space and stay
trivially reversible via the pipeline snapshot), and canvas/viewBox edits touch only the root
``<svg>`` attributes. Complex geometry (outline, boolean path ops, real coordinate baking) is
high risk and out of.

SAFETY (sec.12): every numeric value placed into an attribute goes through
:func:`inkscape_mcp.edit.dom.fmt_num`, which rejects NaN/inf and emits a clean decimal string, so
a non-finite or garbage float can never reach the DOM. Canvas dimensions go through
:func:`normalize_length`, which pattern-checks a CSS length and rejects anything carrying CSS/markup
punctuation. Object ids are used solely for in-tree lookup via :func:`require_target`; they are
never interpolated into argv. No client value is ever string-formatted directly into an attribute.
"""

from __future__ import annotations

import copy
import math
import os
import tempfile
from pathlib import Path

from lxml import etree

from inkscape_mcp.config import Settings, get_settings
from inkscape_mcp.edit.dom import (
    SVG_NS,
    EditError,
    all_ids,
    deep_copy_with_new_ids,
    fmt_num,
    normalize_color,
    normalize_length,
    parse_viewbox,
    prepend_transform,
    require_target,
)
from inkscape_mcp.logging_setup import get_logger
from inkscape_mcp.workspace.subprocess_exec import ProcessError, ProcessResult, run_inkscape

#: Tolerance (user units) for treating a freshly-computed content bbox as equal to the document's
#: current ``viewBox``. The engine's ``--query-all`` bbox carries sub-pixel rounding, so a strict
#: equality check would re-fit (and "change") an already-fitted document every call. Comparing the
#: four numbers within this epsilon makes a second ``fit_to_content`` a true no-op (the viewBox is
#: left byte-identical), so the pipeline reports ``changed=False`` and writes nothing.
_FIT_EPSILON = 1e-6

_logger = get_logger("edit.transform")

#: Hard ceiling on the number of tiles a single ``tile`` call may create (rows * cols). Bounds the
#: in-memory clone count and the output size so a huge grid request can never exhaust memory or
#: produce an unbounded working copy. A 4x4 grid is 16; this leaves generous headroom for real
#: array work while refusing pathological counts.
MAX_TILES = 1024


def _require_finite(name: str, value: float) -> None:
    """Reject a non-finite numeric input with a stable, host-path-free :class:`EditError`."""
    if not math.isfinite(value):
        raise EditError(f"{name} must be finite")


def move(tree: etree._ElementTree, object_id: str, dx: float, dy: float) -> str:
    """Translate the target object by ``(dx, dy)`` in its parent coordinate space.

    Prepends ``translate(dx,dy)`` to the element's transform list. Raises
    :class:`TargetNotFound` if the id is absent and :class:`EditError` on a non-finite offset.
    """
    _require_finite("dx", dx)
    _require_finite("dy", dy)
    elem = require_target(tree.getroot(), object_id)
    prepend_transform(elem, f"translate({fmt_num(dx)},{fmt_num(dy)})")
    return f"translated {object_id!r} by ({fmt_num(dx)},{fmt_num(dy)})"


def scale(tree: etree._ElementTree, object_id: str, sx: float, sy: float | None = None) -> str:
    """Scale the target object by factor ``sx`` (and ``sy``, defaulting to ``sx`` for uniform).

    Scaling is about the origin of the parent coordinate space (acceptable for simple
    transforms; recentering would require baking geometry, which is high risk and out of scope).
    Rejects a non-finite or non-positive factor with :class:`EditError`. Raises
    :class:`TargetNotFound` if the id is absent.
    """
    factor_y = sx if sy is None else sy
    _require_finite("sx", sx)
    _require_finite("sy", factor_y)
    if sx <= 0 or factor_y <= 0:
        raise EditError("scale factors must be positive")
    elem = require_target(tree.getroot(), object_id)
    prepend_transform(elem, f"scale({fmt_num(sx)},{fmt_num(factor_y)})")
    return f"scaled {object_id!r} by ({fmt_num(sx)},{fmt_num(factor_y)}) about the origin"


def rotate(
    tree: etree._ElementTree,
    object_id: str,
    degrees: float,
    cx: float | None = None,
    cy: float | None = None,
) -> str:
    """Rotate the target object by ``degrees``.

    Rotates about ``(cx, cy)`` when BOTH centre coordinates are supplied (``rotate(a,cx,cy)``),
    otherwise about the origin (``rotate(a)``). Supplying only one of ``cx`` / ``cy`` is rejected
    with :class:`EditError`. Raises :class:`TargetNotFound` if the id is absent and
    :class:`EditError` on a non-finite value.
    """
    _require_finite("degrees", degrees)
    if (cx is None) != (cy is None):
        raise EditError("rotation centre requires both cx and cy")
    elem = require_target(tree.getroot(), object_id)
    if cx is not None and cy is not None:
        _require_finite("cx", cx)
        _require_finite("cy", cy)
        prepend_transform(elem, f"rotate({fmt_num(degrees)},{fmt_num(cx)},{fmt_num(cy)})")
        return f"rotated {object_id!r} by {fmt_num(degrees)}deg about ({fmt_num(cx)},{fmt_num(cy)})"
    prepend_transform(elem, f"rotate({fmt_num(degrees)})")
    return f"rotated {object_id!r} by {fmt_num(degrees)}deg about the origin"


def _gen_bg_id(root: etree._Element) -> str:
    """Mint a fresh, unused id for a synthesized bleed-background rect."""
    existing = all_ids(root)
    n = 0
    while True:
        candidate = "bleed-bg" if n == 0 else f"bleed-bg-{n}"
        if candidate not in existing:
            return candidate
        n += 1


def _paint_bleed(
    root: etree._Element, vb: list[float], bleed: float, color: str
) -> tuple[list[float], str]:
    """Extend the document viewBox by ``bleed`` on every side and paint the new strip.

    Grows the four-number ``viewBox`` outward by ``bleed`` user units on each edge (so existing
    geometry keeps its coordinates and stays centred) and inserts a background ``<rect>`` covering
    the whole extended box, painted ``color``, as the FIRST drawable child so it sits behind every
    existing object. Returns ``(new_viewbox_numbers, bg_id)``. The colour is already validated by
    the caller via :func:`normalize_color`; the rect carries only validated numeric + colour tokens
    (sec.12 — no markup can reach an attribute).
    """
    x, y, w, h = vb
    nx, ny = x - bleed, y - bleed
    nw, nh = w + 2 * bleed, h + 2 * bleed
    bg_id = _gen_bg_id(root)
    rect = etree.Element(f"{{{SVG_NS}}}rect")
    rect.set("id", bg_id)
    rect.set("x", fmt_num(nx))
    rect.set("y", fmt_num(ny))
    rect.set("width", fmt_num(nw))
    rect.set("height", fmt_num(nh))
    rect.set("fill", color)
    # Insert as the first child so the bleed strip paints BEHIND all existing content. A leading
    # <defs> is conventionally kept first; insert after it when present so defs stay at the top.
    insert_at = 0
    for idx, child in enumerate(root):
        if isinstance(child.tag, str) and etree.QName(child).localname == "defs":
            insert_at = idx + 1
            break
    root.insert(insert_at, rect)
    return [nx, ny, nw, nh], bg_id


def resize_canvas(
    tree: etree._ElementTree,
    width: str,
    height: str,
    adjust_viewbox: bool = False,
    bleed: float | None = None,
    bleed_color: str = "#ffffff",
) -> str:
    """Set the root ``<svg>`` ``width`` / ``height`` to validated CSS lengths.

    Child geometry is NOT altered. By default the ``viewBox`` is left unchanged when present:
    changing only the canvas dimensions while preserving the existing user-coordinate viewBox is
    the predictable, non-destructive behaviour (the page is rescaled, the drawing's internal
    coordinates are untouched). A viewBox is synthesized only when one is ABSENT, so the document
    gains a sane coordinate system from the new dimensions rather than being left without one.

    With ``adjust_viewbox=True`` the viewBox is RETARGETED to track the new canvas: it becomes
    ``"0 0 W H"`` from the new numeric width/height (so user units map 1:1 to the new page). This
    is opt-in because it changes the coordinate system; the default preserves it. When the new
    width/height are not both finite positive numbers (e.g. a percentage), the existing/synthesized
    behaviour is used and a note is appended.

    With ``bleed`` > 0 (opt-in, default off) the resize ALSO grows the current
    ``viewBox`` outward by ``bleed`` user units on every side and paints that new border strip with
    ``bleed_color`` (a validated colour, default white) via a single background ``<rect>`` behind
    all content — a print-bleed resize done in ONE call rather than needing a second
    ``scale_object`` background step. ``bleed`` mode needs a valid existing/derivable viewBox and is
    mutually exclusive with ``adjust_viewbox`` (which would otherwise re-target the box and discard
    the bleed). ``bleed`` must be finite and >= 0.

    Both inputs go through :func:`normalize_length`, which rejects anything that is not a bare CSS
    length (raising :class:`EditError`), so no markup/CSS punctuation can reach the attribute.
    """
    safe_width = normalize_length(width)
    safe_height = normalize_length(height)
    if bleed is not None and adjust_viewbox:
        raise EditError("resize_canvas bleed cannot be combined with adjust_viewbox")
    if bleed is not None:
        _require_finite("bleed", bleed)
        if bleed < 0:
            raise EditError("resize_canvas bleed must be 0 or greater")
    safe_bleed_color = normalize_color(bleed_color) if bleed else bleed_color
    root = tree.getroot()
    root.set("width", safe_width)
    root.set("height", safe_height)

    if bleed:
        vb = parse_viewbox(root.get("viewBox"))
        if vb is None or not _viewbox_is_valid(vb):
            synthesized = _synthesize_viewbox(safe_width, safe_height)
            vb = parse_viewbox(synthesized) if synthesized is not None else None
        if vb is None:
            raise EditError(
                "resize_canvas bleed needs a valid viewBox (or numeric width/height) to extend"
            )
        new_vb, bg_id = _paint_bleed(root, vb, bleed, safe_bleed_color)
        root.set(
            "viewBox",
            f"{fmt_num(new_vb[0])} {fmt_num(new_vb[1])} {fmt_num(new_vb[2])} {fmt_num(new_vb[3])}",
        )
        return (
            f"resized canvas to {safe_width} x {safe_height} and added a {fmt_num(bleed)}-unit "
            f"bleed border painted {safe_bleed_color} (background {bg_id!r})"
        )

    if adjust_viewbox:
        retargeted = _synthesize_viewbox(safe_width, safe_height)
        if retargeted is not None:
            root.set("viewBox", retargeted)
            return (
                f"resized canvas to {safe_width} x {safe_height} "
                f"and adjusted viewBox to {retargeted!r}"
            )
        return (
            f"resized canvas to {safe_width} x {safe_height} "
            "(viewBox not adjusted: width/height are not numeric)"
        )

    if parse_viewbox(root.get("viewBox")) is None:
        synthesized = _synthesize_viewbox(safe_width, safe_height)
        if synthesized is not None:
            root.set("viewBox", synthesized)
            return (
                f"resized canvas to {safe_width} x {safe_height} "
                f"and synthesized viewBox {synthesized!r}"
            )
    return f"resized canvas to {safe_width} x {safe_height}"


def normalize_viewbox(tree: etree._ElementTree) -> str:
    """Normalize or repair the root ``viewBox``.

    Repair rules:

    - A valid 4-number ``viewBox`` is left untouched (idempotent; reports "already normalized").
    - If ``viewBox`` is ABSENT but numeric ``width`` / ``height`` exist, synthesize
      ``viewBox="0 0 W H"`` from them.
    - If ``viewBox`` is MALFORMED (not four finite numbers, or non-positive width/height),
      repair it from numeric ``width`` / ``height`` when possible, else raise :class:`EditError`.

    Numeric width/height are read by stripping a trailing CSS unit; a width/height that is not a
    finite positive number cannot be used as a repair source.
    """
    root = tree.getroot()
    raw = root.get("viewBox")
    parsed = parse_viewbox(raw)

    if parsed is not None and _viewbox_is_valid(parsed):
        return "viewBox already normalized"

    synthesized = _synthesize_viewbox(root.get("width"), root.get("height"))
    if synthesized is None:
        raise EditError("cannot normalize viewBox: no valid width/height to derive it from")
    root.set("viewBox", synthesized)
    if raw is None:
        return f"synthesized viewBox {synthesized!r} from width/height"
    return f"repaired malformed viewBox to {synthesized!r}"


class ContentBBoxError(Exception):
    """The content bounding box could not be queried from the Inkscape engine.

    Carries a stable, host-path-free public message; the tool layer maps it to ``ToolError``.
    """


def _settings(settings: Settings | None) -> Settings:
    return settings if settings is not None else get_settings()


def _run_query_all(query_path: Path, settings: Settings) -> tuple[float, float, float, float]:
    """Run ``inkscape --query-all <file>`` and parse the root ``<svg>`` union bbox.

    ``shell=False``, arg list only (sec.12); ``query_path`` is a server-created path (the
    sandbox-validated working copy or a server-minted temp probe), never client input. The FIRST
    output line is the root ``<svg>`` element ``id,x,y,w,h`` — the union bounding box of all drawn
    content in the file's output space. Raises :class:`ContentBBoxError` on any failure or a
    degenerate (non-finite / non-positive w,h) box.
    """
    args = ["--query-all", str(query_path)]
    try:
        result: ProcessResult = run_inkscape(args, settings=settings)
    except ProcessError as exc:
        raise ContentBBoxError("inkscape engine unavailable") from exc

    if result.timed_out:
        _logger.error("content bbox query timed out", extra={"duration_s": result.duration_s})
        raise ContentBBoxError("content bbox query timed out")
    if result.returncode != 0:
        _logger.error("content bbox query failed", extra={"returncode": result.returncode})
        raise ContentBBoxError("content bbox query failed")

    first = result.stdout.strip().splitlines()
    if not first:
        raise ContentBBoxError("document has no content to fit the viewBox to")

    # The leading line is the root <svg> union bbox: "id,x,y,w,h".
    fields = first[0].split(",")
    if len(fields) != 5:
        raise ContentBBoxError("content bbox query returned an unexpected shape")
    try:
        x, y, w, h = (float(fields[1]), float(fields[2]), float(fields[3]), float(fields[4]))
    except ValueError as exc:
        raise ContentBBoxError("content bbox query returned a non-numeric value") from exc

    if not all(math.isfinite(v) for v in (x, y, w, h)) or w <= 0 or h <= 0:
        raise ContentBBoxError("content bounding box is degenerate")
    return x, y, w, h


def query_content_bbox(
    working_path: Path, settings: Settings | None = None
) -> tuple[float, float, float, float]:
    """Query a file's CONTENT bounding box via the Inkscape engine (ADR-005).

    Thin wrapper over :func:`_run_query_all` that runs ``--query-all`` on the file as-is. The bbox
    is reported in the file's OUTPUT (pixel) space — i.e. mapped through its ``width``/``height``
    and ``viewBox``. For an idempotent fit use :func:`content_bbox_user_space`, which probes a
    px-identity copy so the box is reported in the document's intrinsic user-coordinate space.

    Raises :class:`ContentBBoxError` if Inkscape is unavailable, the query times out or fails, the
    document has no drawable content, or the bbox is degenerate.
    """
    return _run_query_all(working_path, _settings(settings))


def content_bbox_user_space(
    tree: etree._ElementTree, working_path: Path, settings: Settings | None = None
) -> tuple[float, float, float, float]:
    """Content bbox in the document's INTRINSIC user-coordinate space (idempotent-safe).

    ``--query-all`` reports the union bbox in OUTPUT (pixel) space — it maps the geometry through
    the current ``width``/``height`` AND ``viewBox`` (including ``preserveAspectRatio`` letterbox).
    So fitting the ``viewBox`` to that pixel box and then re-querying yields a DIFFERENT box (the
    new viewBox re-scales the geometry), which made repeated fits non-idempotent.

    Fix: probe a px-IDENTITY copy of the document — same geometry, but ``viewBox`` set to
    ``"0 0 W H"`` from the numeric ``width``/``height`` so user units map 1:1 to pixels. The
    query then reports the content box in the document's own user-coordinate units, a value that
    is STABLE no matter what the current viewBox is. Setting the viewBox to this box and probing
    again returns the same numbers, so a second fit is a true no-op.

    When numeric ``width``/``height`` are unavailable (e.g. a percentage), a px-identity probe
    cannot be built; we fall back to querying the working copy as-is (best-effort, the original
    non-idempotent behaviour) rather than failing the fit. ``working_path`` is used only to derive
    the temp probe's directory implicitly via the OS temp dir — the probe content comes from
    ``tree`` (never client input). Raises :class:`ContentBBoxError` if the bbox cannot be computed.
    """
    s = _settings(settings)
    root = tree.getroot()
    identity = _synthesize_viewbox(root.get("width"), root.get("height"))
    if identity is None:
        # No numeric width/height to build a stable px-identity probe — best-effort fall back to
        # querying the working copy as-is.
        return _run_query_all(working_path, s)

    # Build a px-identity probe from a deep copy of the tree (geometry untouched, viewBox set to
    # "0 0 W H" so user units == pixels) and query THAT. Server-created temp file under the OS temp
    # dir, never client input; removed in the finally.
    probe_root = copy.deepcopy(root)
    probe_root.set("viewBox", identity)
    if "preserveAspectRatio" in probe_root.attrib:
        # Drop letterboxing so the probe reports the geometry box, not an aspect-fitted one.
        del probe_root.attrib["preserveAspectRatio"]
    probe_bytes = etree.tostring(probe_root, xml_declaration=True, encoding="UTF-8")

    fd, tmp_name = tempfile.mkstemp(prefix="inkscape-mcp-fit-", suffix=".svg")
    probe_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(probe_bytes)
        return _run_query_all(probe_path, s)
    finally:
        probe_path.unlink(missing_ok=True)


def fit_to_content(
    tree: etree._ElementTree, working_path: Path, settings: Settings | None = None
) -> str:
    """Set the root ``viewBox`` to the document's CONTENT bounding box (engine-computed).

    The content bbox is computed by the Inkscape engine in the document's intrinsic
    user-coordinate space (:func:`content_bbox_user_space`, ADR-005 — real geometry, not naive
    XML), so the value is STABLE across repeated calls. The root ``<svg>`` ``viewBox`` is set to
    ``"x y w h"`` only when the current viewBox differs from that box beyond :data:`_FIT_EPSILON`;
    when it already matches, the attribute is left byte-identical. The mutation is therefore
    IDEMPOTENT: a second ``fit_to_content`` on an already-fitted document changes nothing, so the
    pipeline reports ``changed=False`` and writes no snapshot/record.

    Child geometry and the original source file are never touched; only the root ``viewBox``
    attribute changes. Raises :class:`ContentBBoxError` if the bbox cannot be computed.
    """
    x, y, w, h = content_bbox_user_space(tree, working_path, settings)
    root = tree.getroot()
    current = parse_viewbox(root.get("viewBox"))
    if current is not None and all(
        abs(a - b) <= _FIT_EPSILON for a, b in zip(current, (x, y, w, h), strict=True)
    ):
        # Already framed to the content (within rounding) — leave the viewBox byte-identical so a
        # repeated fit is a genuine no-op.
        return "viewBox already fits content"
    new_viewbox = f"{fmt_num(x)} {fmt_num(y)} {fmt_num(w)} {fmt_num(h)}"
    root.set("viewBox", new_viewbox)
    return f"fit viewBox to content bbox {new_viewbox!r}"


def tile(
    tree: etree._ElementTree,
    object_id: str,
    rows: int,
    cols: int,
    dx: float,
    dy: float,
) -> str:
    """Lay out an ``rows`` x ``cols`` grid of the target object in ONE operation.

    The cell at grid position ``(r, c)`` is the target deep-copied (via
    :func:`deep_copy_with_new_ids`, which re-ids every id in each clone and rewrites intra-clone
    references) and translated by ``(c*dx, r*dy)`` in the target's parent coordinate space. The
    original object stays in place and IS the ``(0, 0)`` cell, so a ``rows x cols`` grid inserts
    ``rows*cols - 1`` new clones immediately after the original in the same parent. The whole grid
    is produced under one snapshot + one Operation Record (reversible, ADR-004).

    Bounds (sec.12): ``rows`` and ``cols`` must each be integers ``>= 1`` and their product must
    not exceed :data:`MAX_TILES`; ``dx`` / ``dy`` must be finite. A request that fails these is
    rejected with :class:`EditError` before any clone is made. Raises :class:`TargetNotFound` if
    the id is absent.
    """
    if rows < 1 or cols < 1:
        raise EditError("tile rows and cols must each be >= 1")
    if rows * cols > MAX_TILES:
        raise EditError(f"tile grid too large: {rows}x{cols} exceeds {MAX_TILES} cells")
    _require_finite("dx", dx)
    _require_finite("dy", dy)

    elem = require_target(tree.getroot(), object_id)
    parent = elem.getparent()
    if parent is None:
        raise EditError(f"object {object_id!r} cannot be tiled (it is the document root)")

    created = 0
    # Insert each clone immediately after the original (and after the previously inserted clones),
    # advancing the insertion index so the grid stacks in row-major order above the source.
    insert_at = parent.index(elem) + 1
    for r in range(rows):
        for c in range(cols):
            if r == 0 and c == 0:
                # The original object is the (0,0) cell — left in place, untouched.
                continue
            clone, _top_id = deep_copy_with_new_ids(elem, tree.getroot(), None)
            offset_x = c * dx
            offset_y = r * dy
            if offset_x != 0.0 or offset_y != 0.0:
                prepend_transform(clone, f"translate({fmt_num(offset_x)},{fmt_num(offset_y)})")
            parent.insert(insert_at, clone)
            insert_at += 1
            created += 1

    return f"tiled {object_id!r} into a {rows}x{cols} grid ({created} clones added)"


def _viewbox_is_valid(nums: list[float]) -> bool:
    """True iff the four viewBox numbers are finite and width/height are positive."""
    if len(nums) != 4 or not all(math.isfinite(n) for n in nums):
        return False
    return nums[2] > 0 and nums[3] > 0


def _length_to_number(raw: str | None) -> float | None:
    """Parse a CSS length attribute into a finite positive number, or ``None``.

    Strips a trailing CSS unit (``px``, ``pt``, ``mm``, ``%`` ...) so an attribute like
    ``"100px"`` yields ``100.0``. A percentage, non-numeric, non-finite, or non-positive value
    yields ``None`` (it cannot seed a usable ``0 0 W H`` viewBox).
    """
    if raw is None:
        return None
    text = raw.strip()
    if text.endswith("%"):
        return None
    for unit in ("px", "pt", "pc", "mm", "cm", "in", "em", "ex", "rem"):
        if text.endswith(unit):
            text = text[: -len(unit)]
            break
    try:
        value = float(text)
    except ValueError:
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _synthesize_viewbox(width: str | None, height: str | None) -> str | None:
    """Build ``"0 0 W H"`` from numeric width/height, or ``None`` if either is unusable."""
    w = _length_to_number(width)
    h = _length_to_number(height)
    if w is None or h is None:
        return None
    return f"0 0 {fmt_num(w)} {fmt_num(h)}"


__all__ = [
    "MAX_TILES",
    "ContentBBoxError",
    "content_bbox_user_space",
    "fit_to_content",
    "move",
    "normalize_viewbox",
    "query_content_bbox",
    "resize_canvas",
    "rotate",
    "scale",
    "tile",
]
