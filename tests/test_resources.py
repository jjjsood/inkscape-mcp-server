"""Resources over the FastMCP in-memory client: document resources, the documents index, the
runtime/live resources, and the prompt index.

Reading THROUGH the client is the point: handlers once returned pydantic models, which only broke
inside FastMCP's resource wrapper. Each payload must be JSON that re-validates into its source
model; each document resource must equal the matching `inspect_*` engine output.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastmcp import Client
from mcp.shared.exceptions import McpError

from inkscape_mcp.document.inspect import (
    DocAssets,
    DocFonts,
    DocLayers,
    DocObjects,
    DocStyles,
    DocSummary,
    DocTree,
    inspect_assets,
    inspect_fonts,
    inspect_layers,
    inspect_objects,
    inspect_styles,
    inspect_summary,
    inspect_tree,
)
from inkscape_mcp.live.records import LiveOperationLog
from inkscape_mcp.live.session import LiveSession
from inkscape_mcp.live.transport import LiveChange, LiveScene, LiveSelection
from inkscape_mcp.resources.document import documents_index
from inkscape_mcp.runtime.probe import Capabilities
from inkscape_mcp.server import mcp, register_tools
from tests.tool_support import open_doc

register_tools()  # the in-memory client must see every resource (idempotent)

FIXTURE = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
     xmlns:xlink="http://www.w3.org/1999/xlink"
     width="100mm" height="50mm" viewBox="0 0 100 50" inkscape:document-units="mm">
  <defs><style type="text/css">.a { fill: #112233; }</style></defs>
  <g id="layer1" inkscape:groupmode="layer" inkscape:label="L1">
    <rect id="r1" x="0" y="0" width="10" height="10" style="fill:#ff0000;stroke:#00ff00"/>
    <text id="t1" x="5" y="5" font-family="Arial">hello</text>
    <image id="img1" x="0" y="0" width="10" height="10" xlink:href="external.png"/>
  </g>
</svg>
"""

# leaf -> (model, engine function): each resource payload must EQUAL the engine output.
DOC_RESOURCES = {
    "summary": (DocSummary, inspect_summary),
    "tree": (DocTree, inspect_tree),
    "layers": (DocLayers, inspect_layers),
    "objects": (DocObjects, inspect_objects),
    "styles": (DocStyles, inspect_styles),
    "fonts": (DocFonts, inspect_fonts),
    "assets": (DocAssets, inspect_assets),
}
STATIC_RESOURCES = {
    "inkscape://runtime/capabilities": Capabilities,
    "inkscape://live/session": LiveSession,
    "inkscape://live/selection": LiveSelection,
    "inkscape://live/view": LiveScene,
    "inkscape://live/events": LiveChange,
    "inkscape://live/operations": LiveOperationLog,
}


@pytest.fixture
def doc(tool_ws) -> str:
    return open_doc(tool_ws, "fixture.svg", FIXTURE)


def _run(fn):
    async def go() -> Any:
        async with Client(mcp) as client:
            return await fn(client)

    return asyncio.run(go())


def _read(uri: str) -> str:
    return _run(lambda c: c.read_resource(uri))[0].text


def _listed() -> set[str]:
    return {str(r.uri) for r in _run(lambda c: c.list_resources())}


@pytest.mark.parametrize("uri", sorted(STATIC_RESOURCES))
def test_static_resource_reads_and_round_trips(uri: str) -> None:
    text = _read(uri)
    json.loads(text)
    assert STATIC_RESOURCES[uri].model_validate_json(text) is not None


@pytest.mark.parametrize("leaf", sorted(DOC_RESOURCES))
def test_document_resource_equals_the_engine_output(leaf: str, doc: str) -> None:
    model, engine = DOC_RESOURCES[leaf]
    assert model.model_validate_json(_read(f"inkscape://document/{doc}/{leaf}")) == engine(doc)


def test_all_seven_templates_are_registered() -> None:
    templates = {t.uriTemplate for t in _run(lambda c: c.list_resource_templates())}
    assert {f"inkscape://document/{{doc_id}}/{leaf}" for leaf in DOC_RESOURCES} <= templates


@pytest.mark.parametrize("leaf", ["summary", "tree"])
def test_unknown_id_is_a_resource_error(leaf: str, tool_ws) -> None:
    with pytest.raises(McpError, match="not found") as exc:
        _read(f"inkscape://document/d_does_not_exist/{leaf}")
    assert str(tool_ws) not in str(exc.value) and "/tmp" not in str(exc.value)


def test_objects_resource_carries_bbox_and_paint(doc: str) -> None:
    objects = DocObjects.model_validate_json(_read(f"inkscape://document/{doc}/objects"))
    rect = next(o for o in objects.objects if o.id == "r1")
    assert (rect.bbox.width, rect.bbox.height) == (10.0, 10.0)
    assert rect.paint.fill == "#ff0000" and rect.is_leaf is True


def test_tree_bbox_matches_objects_and_leaks_no_host_path(doc: str) -> None:
    text = _read(f"inkscape://document/{doc}/tree")
    tree = DocTree.model_validate_json(text)
    objects = DocObjects.model_validate_json(_read(f"inkscape://document/{doc}/objects"))
    nodes: dict[str, Any] = {}

    def walk(node) -> None:
        if node.id:
            nodes[node.id] = node
        for child in node.children:
            walk(child)

    walk(tree.root)
    for obj in objects.objects:
        if obj.id:
            assert nodes[obj.id].bbox == obj.bbox
    assert "/tmp" not in text and "working" not in text


def test_fonts_resource_names_the_user(doc: str) -> None:
    fonts = DocFonts.model_validate_json(_read(f"inkscape://document/{doc}/fonts"))
    arial = next(f for f in fonts.fonts if f.family == "Arial")
    assert arial.used_by == "t1"


# --- the documents index: how an agent discovers concrete per-doc URIs ---------------------------


def test_documents_index_is_a_listed_static_resource(doc: str) -> None:
    assert "inkscape://documents" in _listed()


def test_documents_index_enumerates_per_doc_uris_and_they_resolve(doc: str) -> None:
    text = _read("inkscape://documents")
    entry = next(d for d in json.loads(text)["documents"] if d["doc_id"] == doc)
    assert entry["resources"] == {
        leaf: f"inkscape://document/{doc}/{leaf}" for leaf in DOC_RESOURCES
    }
    assert "/tmp" not in documents_index() and "working" not in documents_index()
    for uri in entry["resources"].values():
        assert _read(uri)


# --- the prompt index ---------------------------------------------------------------------------


def test_prompts_index_resource_lists_registered_prompts() -> None:
    assert "inkscape://prompts" in _listed()
    payload = json.loads(_read("inkscape://prompts"))
    names = {p["name"] for p in payload["prompts"]}
    assert payload["prompt_count"] == len(payload["prompts"])
    assert {"live_canvas_assist", "compose_artwork", "prepare_web_export"} <= names
    compose = next(p for p in payload["prompts"] if p["name"] == "compose_artwork")
    assert any(a["name"] == "goal" and a["required"] for a in compose["arguments"])
