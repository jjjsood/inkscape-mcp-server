"""Compose tools (E14-03): `set_document_svg` / `insert_svg_fragment`.

Thin MCP layer over the direct-DOM compose engine (:mod:`inkscape_mcp.edit.compose`). These two
tools ADOPT agent-composed SVG into a tracked WORKING COPY — `set_document_svg` replaces the whole
document, `insert_svg_fragment` grafts a fragment under a parent (or the root) — closing the
`Write`-a-file → re-`open_document` loop so an agent never has to round-trip through the filesystem.

HIGH RISK (sec.12 / ADR-004): wholesale or sub-tree replacement from untrusted, free-form SVG is the
highest-risk authoring path. Both tools:

- require a non-empty `approval_token` (mirrors `tools/paths.py`); the shared pipeline passes
  `RiskClass.HIGH` to the policy gate, which refuses the op outright when the token is absent — no
  snapshot/preview/write ever runs unapproved;
- HARDEN the input: the SVG string is safe-parsed (XXE/billion-laughs off) AND every element +
  attribute is checked against a STRICT allowlist — `<script>`, any `on*` handler, `javascript:`
  hrefs, and external (`http(s)://` / `//` / `file:` / `data:`) references are all REJECTED;
- route the mutation through the shared, reversible edit pipeline (`apply_edit`), so the change is
  snapshotted + recorded as an Operation Record + linked to a before/after preview;
- AUTO-RUN `validate_document` after the adopt and fold the findings into the return inline, so the
  agent immediately sees whether the composed document is correct.

Engine / pipeline exceptions map to `ToolError` with stable, host-path-free messages (sec.12):
unknown document → "document id not found"; absent parent id → "object id not found in document";
unparseable/disallowed SVG → its already-safe `ComposeError` message; missing approval → a clear
high-risk message; policy refusal → its safe message.
"""

from __future__ import annotations

from collections.abc import Callable

from fastmcp.exceptions import ToolError
from lxml import etree

from inkscape_mcp.document.inspect import DocumentNotFound, InspectionError, _load_tree
from inkscape_mcp.edit.collection import (
    GridCell,
    make_compose_grid,
    make_place_document,
)
from inkscape_mcp.edit.compose import (
    ComposeError,
    build_blank_svg,
    make_insert_svg_fragment,
    make_set_document_svg,
)
from inkscape_mcp.edit.dom import EditError, TargetNotFound, find_by_id
from inkscape_mcp.edit.pipeline import EditApplyError, EditResult, MutateFn, apply_edit
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.registry import get_registry
from inkscape_mcp.server import mcp
from inkscape_mcp.validate import ValidationReport
from inkscape_mcp.validate import validate_document as _validate_document
from inkscape_mcp.workspace.limits import LimitExceeded, check_input_bytes_size
from inkscape_mcp.workspace.risk import PolicyViolation, RiskClass

_logger = get_logger("tools.compose")


class ComposeResult(EditResult):
    """An :class:`EditResult` extended with the post-adopt `validate_document` findings (E14-03).

    `validation` is the structured report run on the working copy AFTER the SVG was adopted, so an
    agent sees in one call whether the composed document is correct (broken refs, duplicate ids,
    missing viewBox, …). When the adopt was a genuine no-op (`changed=False`) the document is
    unchanged but `validation` still reflects its current state.
    """

    validation: ValidationReport


def _adopt(
    doc_id: str,
    tool: str,
    params: dict[str, object],
    build_mutate: Callable[[], MutateFn],
    *,
    raw_svg: str,
    approval_token: str | None,
) -> ComposeResult:
    """Run one HIGH-risk adopt through the pipeline, then fold in `validate_document` findings.

    Rejects a missing approval token early with a tool-shaped message (the pipeline would also
    refuse via the policy gate). `raw_svg` is byte-size-checked against `max_input_bytes` BEFORE any
    parse, so a multi-hundred-MB string can never exhaust memory in `lxml.fromstring` (the file path
    is gated by `check_input_size`; this is the in-memory-string equivalent — sec.12 DoS guard).
    `build_mutate` is called INSIDE the try block so the eager safe-parse + allowlist scrub (which
    can raise `ComposeError`) is mapped to a stable `ToolError` like a mutation-time failure. Maps
    engine/pipeline exceptions to stable, safe `ToolError`s.
    """
    if not approval_token:
        raise ToolError("high-risk compose operation requires an explicit approval_token")

    try:
        check_input_bytes_size(raw_svg.encode("utf-8"))
    except LimitExceeded as exc:
        raise ToolError("input svg exceeds the configured size limit") from exc

    try:
        result = apply_edit(
            doc_id,
            tool,
            params,
            build_mutate(),
            approval_token=approval_token,
            risk_class=RiskClass.HIGH,
        )
    except (EditApplyError, DocumentNotFound, KeyError) as exc:
        raise ToolError("document id not found") from exc
    except TargetNotFound as exc:
        raise ToolError("object id not found in document") from exc
    except PolicyViolation as exc:
        raise ToolError(str(exc)) from exc
    except ComposeError as exc:
        # ComposeError is an EditError subclass; catch it FIRST so its specific allowlist/href
        # message survives (e.g. "svg contains disallowed element: 'script'").
        raise ToolError(str(exc)) from exc
    except EditError as exc:
        raise ToolError(str(exc)) from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc

    # Auto-run validation on the (now-adopted) working copy and fold the findings in.
    report = _validate_document(doc_id)
    log_tool_call(
        _logger,
        tool=tool,
        doc_id=doc_id,
        operation_id=result.operation_id,
        changed=result.changed,
        validation_ok=report.ok,
        risk_class=RiskClass.HIGH.value,
    )
    return ComposeResult(**result.model_dump(), validation=report)


@mcp.tool
def set_document_svg(doc_id: str, svg: str, approval_token: str | None = None) -> ComposeResult:
    """REPLACE the whole working copy with an agent-composed SVG string (root must be `<svg>`).

    When to use: adopting a full SVG composed in memory, replacing the working copy wholesale (no
    file round-trip). To ADD to (not replace) a document use `insert_svg_fragment`; for a blank
    start use `create_document`.

    Key params: `svg` root must be `<svg>`; it is byte-size-checked, safe-parsed, and
    allowlist-scrubbed — `<script>`, any `on*` handler, `javascript:` hrefs, and external refs
    (`http(s)://` / `//` / `file:` / `data:`) are REJECTED; only a same-document `#id` reference is
    allowed. A real run REQUIRES a non-empty `approval_token`. The original/source file is never
    touched.

    Return shape: `ComposeResult` — an `EditResult` (operation + pre-mutation snapshot links,
    reversible via `restore_snapshot`) extended with the post-adopt `validate_document` findings
    (`validation`).

    Example: `set_document_svg(doc_id, "<svg ...>...</svg>", approval_token="ok")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: HIGH — requires a non-empty `approval_token`; without it the op is refused and
    nothing is written.
    """
    return _adopt(
        doc_id,
        "set_document_svg",
        {"svg_length": len(svg)},
        lambda: make_set_document_svg(svg),
        raw_svg=svg,
        approval_token=approval_token,
    )


@mcp.tool
def insert_svg_fragment(
    doc_id: str,
    svg: str,
    parent_id: str | None = None,
    unwrap: bool = True,
    approval_token: str | None = None,
) -> ComposeResult:
    """Insert an agent-composed SVG fragment under a parent (`parent_id`) or the document root.

    When to use: grafting one composed subtree into an existing document (no file round-trip). To
    REPLACE the whole document use `set_document_svg`; for a single typed shape use the `create_*`
    tools.

    Key params: `svg` is ONE element subtree (wrap several siblings in a `<g>`). `parent_id` (must
    exist) sets where it lands, else the document root. `unwrap` (default `True`) controls a `<svg>`
    root: when `True` the wrapper `<svg>` is unwrapped and its children grafted (an empty wrapper is
    rejected); pass `unwrap=False` to KEEP an explicit nested `<svg>` container, inserted as-is
    (still allowlist-scrubbed; an empty nested `<svg>` is then allowed). `unwrap` has no effect on a
    non-`<svg>` root, which is always inserted intact. Same hardening as `set_document_svg`
    (safe-parse + strict allowlist; `<script>`, `on*` handlers, `javascript:` hrefs, external refs
    rejected; only same-document `#id` allowed). A real run REQUIRES a non-empty `approval_token`.
    The original/source file is never touched.

    Return shape: `ComposeResult` — an `EditResult` (operation + pre-mutation snapshot links,
    reversible via `restore_snapshot`) extended with the post-adopt `validate_document` findings
    (`validation`).

    Example: `insert_svg_fragment(doc_id, "<g>...</g>", parent_id="layer1", approval_token="ok")`;
    to keep a nested container: `insert_svg_fragment(doc_id, "<svg>...</svg>", unwrap=False,
    approval_token="ok")`.

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: HIGH — requires a non-empty `approval_token`; without it the op is refused and
    nothing is written.
    """
    return _adopt(
        doc_id,
        "insert_svg_fragment",
        {"svg_length": len(svg), "parent_id": parent_id, "unwrap": unwrap},
        lambda: make_insert_svg_fragment(svg, parent_id=parent_id, unwrap=unwrap),
        raw_svg=svg,
        approval_token=approval_token,
    )


class ComposeGridResult(EditResult):
    """An :class:`EditResult` for `compose_grid` extended with the grid layout (E16-05).

    `target_doc_id` is the document the sheet was composed INTO — a freshly created blank document
    when `target_doc_id` was not supplied (so the caller learns the new id), else the one passed in.
    `cells` is the ordered placement plan (one :class:`GridCell` per asset: grid coords + the new
    cell-group id + a short source label), and `rows`/`cols` echo the grid shape.
    """

    target_doc_id: str
    rows: int
    cols: int
    cells: list[GridCell]


def _resolve_grid_assets(
    *,
    doc_ids: list[str] | None,
    object_ids: list[str] | None,
    source_doc_id: str | None,
) -> list[tuple[etree._Element, str]]:
    """Resolve the ordered `(source_element, label)` pairs for `compose_grid` from one input mode.

    EXACTLY ONE source mode is allowed (ADR-002 — a small, unambiguous tool, not a dispatcher):

    - `doc_ids`: each whole document's ROOT element is the asset (one cell per document). The root
      is grafted as-is; the placement engine deep-copies + re-ids it, so the source working copies
      are never mutated.
    - `object_ids` (+ `source_doc_id`): each named object in the ONE `source_doc_id` is an asset.

    Reads each source through `_load_tree` (safe parse). Raises `ToolError` with a stable,
    host-path-free message for a bad mode combination, an unknown id, or a missing object.
    """
    has_docs = bool(doc_ids)
    has_objects = bool(object_ids)
    if has_docs == has_objects:
        raise ToolError(
            "compose_grid requires EXACTLY ONE of doc_ids OR object_ids (with source_doc_id)"
        )

    pairs: list[tuple[etree._Element, str]] = []
    if has_docs:
        if source_doc_id is not None:
            raise ToolError("source_doc_id is only valid with object_ids, not doc_ids")
        for did in doc_ids or []:
            try:
                _entry, root = _load_tree(did)
            except DocumentNotFound as exc:
                raise ToolError("document id not found") from exc
            except InspectionError as exc:
                raise ToolError("document could not be parsed safely") from exc
            pairs.append((root, did))
        return pairs

    if source_doc_id is None:
        raise ToolError("object_ids requires source_doc_id (the document the objects live in)")
    try:
        _entry, root = _load_tree(source_doc_id)
    except DocumentNotFound as exc:
        raise ToolError("document id not found") from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc
    for oid in object_ids or []:
        elem = find_by_id(root, oid)
        if elem is None:
            raise ToolError("object id not found in document")
        pairs.append((elem, oid))
    return pairs


@mcp.tool
def compose_grid(
    rows: int,
    cols: int,
    cell: float,
    doc_ids: list[str] | None = None,
    object_ids: list[str] | None = None,
    source_doc_id: str | None = None,
    target_doc_id: str | None = None,
    gap: float = 0.0,
    padding: float = 0.0,
    scale_to_fit: bool = True,
) -> ComposeGridResult:
    """Lay out N DIFFERENT assets in a grid (contact / spec sheet) in ONE reversible call.

    When to use: building a multi-asset sheet — one cell per DIFFERENT document or object — in a
    single call (no per-asset loop, no lxml subtree extract). To repeat ONE object into a grid use
    `tile`; to graft a single composed subtree use `insert_svg_fragment`.

    Key params: supply EXACTLY ONE source mode — `doc_ids` (one whole document per cell) OR
    `object_ids` together with `source_doc_id` (objects from one document per cell). The grid fills
    ROW-MAJOR over `rows` x `cols` cells of size `cell` (user units); fewer assets than cells leaves
    trailing cells empty. Each asset is deep-copied (every id re-minted, intra-clone refs rewritten,
    no id clashes), wrapped in a `<g>` translated to its cell origin and, with `scale_to_fit`
    (default True), uniformly DOWN-scaled to fit `cell - 2*padding` (never upscaled).
    `gap`/`padding` (default 0) space the cells. `target_doc_id` composes INTO an existing document;
    omit it to create a new blank document sized to the whole grid. Bounded: `rows*cols` ≤ the
    engine cell cap; the asset count must not exceed the cell count.

    Return shape: `ComposeGridResult` — an `EditResult` (one `operation_id` + one pre-mutation
    snapshot for the whole sheet, reversible via `restore_snapshot`) plus `target_doc_id` (the new
    id when a blank doc was created), `rows`/`cols`, and `cells` (the ordered placement plan: per
    asset its grid coords, new cell-group id, and a short source label).

    Example: `compose_grid(3, 4, 64, doc_ids=["d1","d2",...,"d12"])` lays a 12-icon system into a
    3x4 sheet in one call.

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium (creates a new tracked document or composes into one; sources never mutated).
    """
    assets = _resolve_grid_assets(
        doc_ids=doc_ids, object_ids=object_ids, source_doc_id=source_doc_id
    )

    # Build the placement closure (validates rows/cols/cell/asset-count BEFORE any side effect).
    try:
        mutate, plan = make_compose_grid(
            assets,
            rows,
            cols,
            cell,
            gap=gap,
            padding=padding,
            scale_to_fit=scale_to_fit,
        )
    except EditError as exc:
        raise ToolError(str(exc)) from exc

    # Resolve the target: compose into a given doc, else create a blank sheet sized to the grid.
    if target_doc_id is not None:
        # Fail fast with a stable message if the id does not resolve / parse, BEFORE any side
        # effect.
        _require_doc(target_doc_id)
        dest_doc_id = target_doc_id
    else:
        stride_w = cols * cell + max(0, cols - 1) * gap + 2 * padding
        stride_h = rows * cell + max(0, rows - 1) * gap + 2 * padding
        try:
            svg_bytes = build_blank_svg(stride_w, stride_h)
        except (
            ComposeError
        ) as exc:  # pragma: no cover - sizes already validated by make_compose_grid
            raise ToolError(str(exc)) from exc
        try:
            entry = get_registry().create_document(svg_bytes)
        except LimitExceeded as exc:
            raise ToolError("input exceeds the configured size limit") from exc
        dest_doc_id = entry.doc_id

    try:
        result = apply_edit(
            dest_doc_id,
            "compose_grid",
            {"rows": rows, "cols": cols, "cell": cell, "assets": len(assets)},
            mutate,
        )
    except (EditApplyError, DocumentNotFound, KeyError) as exc:
        raise ToolError("document id not found") from exc
    except TargetNotFound as exc:
        raise ToolError("object id not found in document") from exc
    except EditError as exc:
        raise ToolError(str(exc)) from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc

    log_tool_call(
        _logger,
        tool="compose_grid",
        doc_id=dest_doc_id,
        operation_id=result.operation_id,
        cells=len(plan),
    )
    return ComposeGridResult(
        **result.model_dump(),
        target_doc_id=dest_doc_id,
        rows=rows,
        cols=cols,
        cells=plan,
    )


def _require_doc(doc_id: str) -> None:
    """Verify `doc_id` resolves to a parseable working tree, or raise a stable `ToolError`.

    Used by `compose_grid` to fail fast with a host-path-free message when an explicit
    `target_doc_id` does not resolve, before any side effect (snapshot/write) runs.
    """
    try:
        _load_tree(doc_id)
    except DocumentNotFound as exc:
        raise ToolError("document id not found") from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc


class PlaceResult(EditResult):
    """An :class:`EditResult` for `place_document` (E16-10a).

    `placed_id` is the id of the new `<g>` cell wrapper grafted into the TARGET document (it holds
    the deep-copied, re-id'd source subtree), so the caller can immediately move/style/export it.
    `target_doc_id` echoes the document placed INTO and `source` is a short, host-path-free label
    for what was placed (the source `doc_id` or `object_id`).
    """

    target_doc_id: str
    placed_id: str
    source: str


@mcp.tool
def place_document(
    target_doc_id: str,
    x: float,
    y: float,
    source_doc_id: str | None = None,
    object_id: str | None = None,
    scale: float = 1.0,
) -> PlaceResult:
    """Place an existing document or object INTO another document at `(x, y)` with `scale`.

    When to use: re-composing existing geometry cross-document without re-authoring or extracting
    SVG by hand — the single-asset companion of `compose_grid`. To lay out MANY assets in a grid use
    `compose_grid`; to graft agent-COMPOSED markup use `insert_svg_fragment`; to instance a
    same-document object use `create_use`.

    Key params: supply EXACTLY ONE source — `source_doc_id` (place that whole document's root) OR
    `object_id` together with `source_doc_id` (place that one object from the source document). The
    source subtree is deep-copied (every id re-minted, intra-clone refs rewritten, no id clashes —
    the source is NEVER mutated) and wrapped in a `<g>` translated to `(x, y)` and uniformly scaled
    by `scale` (> 0) about that origin. The whole place lands under ONE snapshot + Operation Record.

    Return shape: `PlaceResult` — an `EditResult` (reversible via `restore_snapshot`) plus
    `target_doc_id`, `placed_id` (the new wrapper-group id), and `source` (a short label).

    Example: `place_document("sheet", 100, 0, source_doc_id="logo")` drops the whole `logo` document
    into `sheet` at (100, 0); `place_document("sheet", 0, 0, source_doc_id="kit", object_id="star")`
    places just the `star` object from `kit`.

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium (write-new on the target working copy, reversible; sources never mutated).
    """
    # Source mode (ADR-002): a whole source document, or one object from one source doc.
    if not source_doc_id:
        raise ToolError("place_document requires source_doc_id (the document to place from)")
    try:
        _entry, source_root = _load_tree(source_doc_id)
    except DocumentNotFound as exc:
        raise ToolError("document id not found") from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc

    if object_id is not None:
        source_elem = find_by_id(source_root, object_id)
        if source_elem is None:
            raise ToolError("object id not found in document")
        label = object_id
    else:
        source_elem = source_root
        label = source_doc_id

    # Fail fast on an unresolvable target BEFORE any side effect (snapshot/write).
    _require_doc(target_doc_id)

    holder: list[str] = []
    try:
        mutate = make_place_document(source_elem, label, x, y, scale=scale, result_holder=holder)
    except EditError as exc:
        raise ToolError(str(exc)) from exc

    try:
        result = apply_edit(
            target_doc_id,
            "place_document",
            {"source": label, "x": x, "y": y, "scale": scale},
            mutate,
        )
    except (EditApplyError, DocumentNotFound, KeyError) as exc:
        raise ToolError("document id not found") from exc
    except TargetNotFound as exc:
        raise ToolError("object id not found in document") from exc
    except EditError as exc:
        raise ToolError(str(exc)) from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc

    placed_id = holder[0] if holder else ""
    log_tool_call(
        _logger,
        tool="place_document",
        doc_id=target_doc_id,
        operation_id=result.operation_id,
        placed_id=placed_id,
    )
    return PlaceResult(
        **result.model_dump(),
        target_doc_id=target_doc_id,
        placed_id=placed_id,
        source=label,
    )
