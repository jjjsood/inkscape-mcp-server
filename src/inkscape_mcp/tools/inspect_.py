"""`inspect`: read-only view of one or several documents (replaces the old inspect/find/validate/
quality/snapshot-list/frame-list tools).
"""

from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from pydantic import BaseModel

from inkscape_mcp import quality as quality_engine
from inkscape_mcp import snapshots as snapshots_engine
from inkscape_mcp.document import inspect as inspect_engine
from inkscape_mcp.document.inspect import ObjectRef
from inkscape_mcp.edit import collection as collection_engine
from inkscape_mcp.edit.ops import Selector
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.render.cli import list_frames
from inkscape_mcp.tools._result import guarded, ok, require_doc
from inkscape_mcp.validate import validate_document

_logger = get_logger("tools.inspect")

Section = Literal[
    "summary",
    "objects",
    "tree",
    "layers",
    "styles",
    "fonts",
    "assets",
    "validate",
    "quality",
    "snapshots",
    "frames",
]
Detail = Literal["ids", "summary", "full"]

DEFAULT_LIMIT = 200

DESCRIPTION = (
    "Read-only view of one document (or several: pass a list of doc_ids). `include` picks "
    "sections: summary (default), objects, tree, layers, styles, fonts, assets, validate, quality, "
    "snapshots, frames. quality = validity/hygiene, not content; several doc_ids add "
    "consistency (viewBox, stroke, id naming). `select` filters `objects` "
    "(tag, fill, stroke, text, id_prefix, bbox) so "
    "you need no separate find call; giving only `select` returns just the objects, as "
    "[id, tag, bbox, fill] rows capped by `limit` (truncated=true when cut). "
    "detail: ids | summary | full (full adds stroke, text, transform). "
    "Example: inspect(doc_id, select={'tag': 'rect'})"
)


def _object_row(ref: ObjectRef, detail: Detail) -> Any:
    bbox = ref.bbox
    box = [bbox.x, bbox.y, bbox.width, bbox.height] if bbox is not None else None
    if detail == "ids":
        return ref.object_id
    if detail == "full":
        row: dict[str, Any] = {"id": ref.object_id, "tag": ref.tag, "bbox": box}
        row.update(fill=ref.fill, stroke=ref.stroke, text=ref.text)
        if ref.transform is not None:
            row["transform"] = ref.transform
        return row
    return [ref.object_id, ref.tag, box, ref.fill]


def _objects(doc_id: str, select: Selector | None, detail: Detail, limit: int) -> dict[str, Any]:
    if select is None:
        refs = inspect_engine.list_objects(doc_id)
    else:
        refs = inspect_engine.find_objects(
            doc_id,
            tag=select.tag,
            fill=select.fill,
            stroke=select.stroke,
            text=select.text,
            id_prefix=select.id_prefix,
            bbox=select.bbox,
            accurate_bbox=select.accurate_bbox,
        ).objects
    total = len(refs)
    shown = refs[:limit]
    out: dict[str, Any] = {
        "count": total,
        "objects": [_object_row(r, detail) for r in shown],
    }
    if total > len(shown):
        out["truncated"] = True
    if detail == "summary":
        out["columns"] = ["id", "tag", "bbox", "fill"]
    return out


def _dump(model: BaseModel) -> Any:
    return model.model_dump(mode="json", exclude_none=True)


def _sections(
    doc_id: str,
    include: list[Section],
    select: Selector | None,
    detail: Detail,
    limit: int,
    series: str | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in include:
        if name == "summary":
            out["summary"] = _dump(inspect_engine.inspect_summary(doc_id))
            if stale := collection_engine.stale_grid_sources(doc_id):
                out["summary"]["stale_sources"] = stale
        elif name == "objects":
            out["objects"] = _objects(doc_id, select, detail, limit)
        elif name == "tree":
            out["tree"] = _dump(inspect_engine.inspect_tree(doc_id))
        elif name == "layers":
            out["layers"] = _dump(inspect_engine.inspect_layers(doc_id))
        elif name == "styles":
            out["styles"] = _dump(inspect_engine.inspect_styles(doc_id))
        elif name == "fonts":
            out["fonts"] = _dump(inspect_engine.inspect_fonts(doc_id))
        elif name == "assets":
            out["assets"] = _dump(inspect_engine.inspect_assets(doc_id))
        elif name == "validate":
            out["validate"] = _dump(validate_document(doc_id))
        elif name == "quality":
            out["quality"] = _dump(quality_engine.quality_report(doc_id))
        elif name == "snapshots":
            out["snapshots"] = [_dump(s) for s in snapshots_engine.list_snapshots(doc_id)]
        elif name == "frames":
            out["frames"] = _dump(list_frames(doc_id, series))
    return out


@guarded()
def inspect_(
    doc_id: str | list[str],
    include: list[Section] | None = None,
    select: Selector | None = None,
    detail: Detail = "summary",
    limit: int = DEFAULT_LIMIT,
    series: str | None = None,
) -> ToolResult:
    ids = [doc_id] if isinstance(doc_id, str) else list(doc_id)
    if not ids:
        raise ToolError("doc_id must name at least one document")
    if limit < 1:
        raise ToolError("limit must be >= 1")
    sections: list[Section] = list(include) if include else (["objects"] if select else ["summary"])
    if select is not None and "objects" not in sections:
        sections.append("objects")
    for one in ids:  # fail on an unknown id before doing any work
        require_doc(one)

    if isinstance(doc_id, str):
        payload: dict[str, Any] = {"doc_id": doc_id}
        payload.update(_sections(doc_id, sections, select, detail, limit, series))
    else:
        quality_engine.require_unique_doc_ids(ids)
        docs = {one: _sections(one, sections, select, detail, limit, series) for one in ids}
        payload = {"docs": docs}
        if "quality" in sections:
            payload["consistency"] = _dump(quality_engine.build_set_verdict(ids))
    log_tool_call(_logger, tool="inspect", docs=len(ids), include=sections)
    return ok(payload)


def register(mcp: FastMCP) -> None:
    mcp.tool(name="inspect", description=DESCRIPTION, output_schema=None)(inspect_)
