"""Document-resource tests: registration, parity with inspect_*, error mapping."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastmcp.exceptions import ResourceError

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
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
from inkscape_mcp.registry import reset_registry
from inkscape_mcp.resources.document import (
    document_assets,
    document_fonts,
    document_layers,
    document_objects,
    document_styles,
    document_summary,
    document_tree,
    documents_index,
)
from inkscape_mcp.server import mcp
from inkscape_mcp.tools.document import open_document

FIXTURE_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
     xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.0.dtd"
     xmlns:xlink="http://www.w3.org/1999/xlink"
     width="100mm" height="50mm" viewBox="0 0 100 50"
     inkscape:document-units="mm">
  <defs>
    <style type="text/css">.a { fill: #112233; }</style>
  </defs>
  <g id="layer1" inkscape:groupmode="layer" inkscape:label="L1">
    <rect id="r1" x="0" y="0" width="10" height="10" style="fill:#ff0000;stroke:#00ff00"/>
    <text id="t1" x="5" y="5" font-family="Arial">hello</text>
    <image id="img1" x="0" y="0" width="10" height="10" xlink:href="external.png"/>
  </g>
</svg>
"""

# (resource function, matching inspection function, model) triples — the parity contract.
# Resources now return JSON strings (FastMCP resource-return contract), so the JSON is
# re-validated back into the source model before comparing to the inspect_* output.
_PARITY = [
    (document_summary, inspect_summary, DocSummary),
    (document_tree, inspect_tree, DocTree),
    (document_layers, inspect_layers, DocLayers),
    (document_objects, inspect_objects, DocObjects),
    (document_styles, inspect_styles, DocStyles),
    (document_fonts, inspect_fonts, DocFonts),
    (document_assets, inspect_assets, DocAssets),
]

_EXPECTED_TEMPLATES = {
    "inkscape://document/{doc_id}/summary",
    "inkscape://document/{doc_id}/tree",
    "inkscape://document/{doc_id}/layers",
    "inkscape://document/{doc_id}/objects",
    "inkscape://document/{doc_id}/styles",
    "inkscape://document/{doc_id}/fonts",
    "inkscape://document/{doc_id}/assets",
}


@pytest.fixture
def doc_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(root))
    get_settings.cache_clear()
    reset_registry()

    src = root / "fixture.svg"
    src.write_bytes(FIXTURE_SVG)

    return open_document(str(src)).doc_id


def _template_uris() -> set[str]:
    templates = asyncio.run(mcp.list_resource_templates())
    return {t.uri_template for t in templates}


def _static_resource_uris() -> set[str]:
    resources = asyncio.run(mcp.list_resources())
    return {str(r.uri) for r in resources}


def test_all_seven_resources_resolve(doc_id: str) -> None:
    """Every resource function resolves (returns valid JSON for its model) for an opened doc."""
    for resource_fn, _, model in _PARITY:
        assert model.model_validate_json(resource_fn(doc_id)) is not None


@pytest.mark.parametrize(
    ("resource_fn", "inspect_fn", "model"),
    _PARITY,
    ids=[r.__name__ for r, _, _ in _PARITY],
)
def test_resource_payload_matches_inspect(resource_fn, inspect_fn, model, doc_id: str) -> None:
    """Parity: each resource payload (decoded) EQUALS the matching inspect_* output for the id."""
    assert model.model_validate_json(resource_fn(doc_id)) == inspect_fn(doc_id)


def test_unknown_id_summary_raises_resource_error(doc_id: str) -> None:
    with pytest.raises(ResourceError) as exc:
        document_summary("d_does_not_exist")
    assert "not found" in str(exc.value)


def test_unknown_id_tree_raises_resource_error(doc_id: str) -> None:
    with pytest.raises(ResourceError) as exc:
        document_tree("d_does_not_exist")
    assert "not found" in str(exc.value)


def test_seven_templates_registered_on_mcp(doc_id: str) -> None:
    """All seven document resource templates are registered on the shared app."""
    assert _EXPECTED_TEMPLATES <= _template_uris()


# --- /: new read-surface fields + index discoverability ---------------


def test_objects_resource_carries_bbox_and_paint(doc_id: str) -> None:
    """The objects resource payload includes per-object bbox + paint (R5 / S12 / S13)."""
    payload = DocObjects.model_validate_json(document_objects(doc_id))
    rect = next(o for o in payload.objects if o.id == "r1")
    assert rect.bbox is not None
    assert (rect.bbox.width, rect.bbox.height) == (10.0, 10.0)
    assert rect.paint.fill == "#ff0000"
    assert rect.is_leaf is True


def test_tree_resource_carries_bbox_and_matches_objects(doc_id: str) -> None:
    """The tree resource carries per-element bbox, identical to the objects resource (parity)."""
    tree = DocTree.model_validate_json(document_tree(doc_id))
    objects = DocObjects.model_validate_json(document_objects(doc_id))

    nodes: dict[str, object] = {}

    def walk(node) -> None:
        if node.id:
            nodes[node.id] = node
        for child in node.children:
            walk(child)

    walk(tree.root)

    rect = nodes["r1"]
    assert rect.bbox is not None  # type: ignore[union-attr]
    assert (rect.bbox.width, rect.bbox.height) == (10.0, 10.0)  # type: ignore[union-attr]

    # Cross-resource parity: tree bbox equals objects bbox for every identified element.
    for obj in objects.objects:
        if obj.id:
            assert nodes[obj.id].bbox == obj.bbox  # type: ignore[union-attr]

    # Host-path-free: no absolute path leaks into the tree payload.
    assert "/tmp" not in document_tree(doc_id)
    assert "working" not in document_tree(doc_id)


def test_fonts_resource_carries_available_and_used_by(doc_id: str) -> None:
    """The fonts resource payload flags availability and the referencing element id (D8/R7)."""
    payload = DocFonts.model_validate_json(document_fonts(doc_id))
    arial = next(f for f in payload.fonts if f.family == "Arial")
    assert arial.used_by == "t1"
    # available is a tri-state bool|None; the field exists regardless of the host font database.
    assert arial.available in (True, False, None)


def test_documents_index_is_a_static_resource(doc_id: str) -> None:
    """the index is a concrete (non-template) resource so ListMcpResourcesTool sees it."""
    assert "inkscape://documents" in _static_resource_uris()


def test_documents_index_lists_per_doc_resource_uris(doc_id: str) -> None:
    """The index enumerates the open doc id and its seven concrete per-doc resource URIs."""
    payload = json.loads(documents_index())
    docs = payload["documents"]
    entry = next(d for d in docs if d["doc_id"] == doc_id)
    expected = {
        leaf: f"inkscape://document/{doc_id}/{leaf}"
        for leaf in (
            "summary",
            "tree",
            "layers",
            "objects",
            "styles",
            "fonts",
            "assets",
        )
    }
    assert entry["resources"] == expected
    # Host-path-free: no absolute path leaks into the index payload.
    assert "/tmp" not in documents_index()
    assert "working" not in documents_index()
