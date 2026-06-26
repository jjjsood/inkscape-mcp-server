"""Object-discovery tool (E14-07): ``find_objects``.

Thin MCP layer over the read-only `find_objects` engine in :mod:`inkscape_mcp.document.inspect`.
Its purpose is to make the id-taking edit tools (`set_fill`, `move_object`, `replace_text`, …)
usable on documents the agent did NOT author: given paint / tag / text / id-prefix / bbox filters,
it returns the addressable object ids that match. Direct DOM only (ADR-005), read-only — so there
is NO pipeline, snapshot, or Operation Record (low risk, nothing is mutated).

This layer maps engine exceptions to `ToolError` with STABLE, host-path-free messages (sec.12): an
unknown document id becomes ``"document id not found"`` and an unparseable working copy becomes
``"document could not be parsed safely"``. Object ids are used only for in-tree lookup, never argv.
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError

from inkscape_mcp.document.inspect import (
    BBox,
    DocumentNotFound,
    FindResult,
    InspectionError,
)
from inkscape_mcp.document.inspect import (
    find_objects as _find_objects,
)
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.server import mcp

_logger = get_logger("tools.find")


@mcp.tool
def find_objects(
    doc_id: str,
    tag: str | None = None,
    fill: str | None = None,
    stroke: str | None = None,
    text: str | None = None,
    id_prefix: str | None = None,
    bbox: BBox | None = None,
    accurate_bbox: bool = False,
) -> FindResult:
    """Find addressable object ids in a tracked document by tag / paint / text / id-prefix / bbox.

    When to use: before an id-taking edit (`set_fill`, `move_object`, `replace_text`,
    `rotate_object`, …) on a document the agent did not author, or to enumerate "every blue rect",
    "every text mentioning 'Total'", etc. For the full structural picture (tree / layers / styles /
    fonts / assets) plus the same list use `inspect_document`; to map a goal to a tool, `how_do_i`.

    Key params (all filters optional; supplied filters AND together; none → every addressable
    object): `tag` exact local name (`"rect"`/`"text"`/`"path"`); `fill`/`stroke` a paint matched
    casing- and hex-shorthand-insensitive (`"#FFF"` matches `"#ffffff"`) — matching resolves the
    FULL CSS cascade so an object painted via a `<style>` rule / class / id selector or INHERITED
    from an ancestor `<g>` is matched too (the reported `fill`/`stroke` stay the per-element
    authored token); `text` a case-insensitive substring of text content; `id_prefix` an `id`
    prefix; `bbox` an `{x, y, width, height}` box kept on INTERSECTION. By default `bbox` uses the
    attribute-derived box and objects with no derivable box (path/text/group/transformed) are
    EXCLUDED; set `accurate_bbox=true` to compute geometry-accurate, transform-/outline-aware boxes
    via one batched Inkscape `--query-all` call (so those objects can match) — it degrades to the
    attribute box when the Inkscape engine is unavailable.

    Return shape: `FindResult` — `{doc_id, count, objects: [{object_id, tag, bbox?, fill?, stroke?,
    text?}]}`. Objects without an `id` are never returned (they cannot be targeted). With
    `accurate_bbox=true`, `bbox` carries the engine box where one was reported.

    Example: `find_objects("d_ab12", tag="rect", fill="#3366cc")`

    Risk class: low for the default direct-DOM path (read-only, ADR-005; no snapshot / Operation
    Record); `accurate_bbox=true` adds a read-only Inkscape `--query-all` invocation (medium, still
    no mutation).
    """
    try:
        result = _find_objects(
            doc_id,
            tag=tag,
            fill=fill,
            stroke=stroke,
            text=text,
            id_prefix=id_prefix,
            bbox=bbox,
            accurate_bbox=accurate_bbox,
        )
    except DocumentNotFound as exc:
        raise ToolError("document id not found") from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc

    log_tool_call(_logger, tool="find_objects", doc_id=doc_id, count=result.count)
    return result
