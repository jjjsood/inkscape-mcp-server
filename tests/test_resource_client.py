"""Resource-client regression tests (E9-02).

Locks the E9-01 resource-serialization fix at the layer where it broke: the FastMCP resource
WRAPPER, not the inspection engine. Pre-fix, every resource handler returned its pydantic model
object; reading any resource over the FastMCP in-memory `Client` raised
``contents must be str, bytes, or list[ResourceContent], got <Model>`` (a `TypeError` surfaced as
an `McpError`). The unit suite never caught it because no test read a resource THROUGH the client.

These tests read all 13 resource URIs over the in-memory `Client` and assert, per resource:
1. the read succeeds (no serialization error),
2. the payload text parses as JSON,
3. the JSON re-validates into the resource's source pydantic model,
4. (document resources only) the decoded payload EQUALS the matching `inspect_*` component — the
   parity contract carried over from `test_document_resources.py`, now exercised end-to-end.

Async is run via `asyncio.run(...)` inside sync test functions, matching the repo convention
(see `test_document_resources.py`); no async plugin/config is added.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastmcp import Client

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
from inkscape_mcp.live.records import LiveOperationLog
from inkscape_mcp.live.session import LiveSession
from inkscape_mcp.live.transport import LiveChange, LiveScene, LiveSelection
from inkscape_mcp.registry import reset_registry
from inkscape_mcp.runtime.probe import Capabilities
from inkscape_mcp.server import mcp, register_tools
from inkscape_mcp.tools.document import open_document

# Register the full tool/resource surface against the shared app once, at import time, so the
# in-memory client sees every resource (idempotent: the decorators tolerate re-registration).
register_tools()

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

# Static (non-document) resource URIs and the model each payload must re-validate into.
_STATIC_RESOURCES = [
    ("inkscape://runtime/capabilities", Capabilities),
    ("inkscape://live/session", LiveSession),
    ("inkscape://live/selection", LiveSelection),
    ("inkscape://live/view", LiveScene),
    ("inkscape://live/events", LiveChange),
    ("inkscape://live/operations", LiveOperationLog),
]

# Document resource leaf -> (model, matching inspection function). The parity contract: each
# resource payload, decoded, must equal the corresponding `inspect_*` output for the same doc_id.
_DOC_RESOURCES = [
    ("summary", DocSummary, inspect_summary),
    ("tree", DocTree, inspect_tree),
    ("layers", DocLayers, inspect_layers),
    ("objects", DocObjects, inspect_objects),
    ("styles", DocStyles, inspect_styles),
    ("fonts", DocFonts, inspect_fonts),
    ("assets", DocAssets, inspect_assets),
]


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


def _read_text(uri: str) -> str:
    """Read a resource over the FastMCP in-memory client and return its payload text.

    This is the exact path that broke pre-E9-01: a handler returning a model (not a str) raises
    here with ``contents must be str, bytes, or list[ResourceContent]``.
    """

    async def _run() -> str:
        async with Client(mcp) as client:
            res = await client.read_resource(uri)
        return res[0].text

    return asyncio.run(_run())


@pytest.mark.parametrize(
    ("uri", "model"),
    _STATIC_RESOURCES,
    ids=[uri for uri, _ in _STATIC_RESOURCES],
)
def test_static_resource_reads_and_round_trips(uri: str, model: type) -> None:
    """Each static resource reads cleanly over the client, is JSON, and round-trips into a model."""
    text = _read_text(uri)
    json.loads(text)  # parses as JSON (raises on malformed payload)
    assert model.model_validate_json(text) is not None


@pytest.mark.parametrize(
    ("leaf", "model", "inspect_fn"),
    _DOC_RESOURCES,
    ids=[leaf for leaf, _, _ in _DOC_RESOURCES],
)
def test_document_resource_reads_round_trips_and_matches_inspect(
    leaf: str, model: type, inspect_fn, doc_id: str
) -> None:
    """Each document resource reads over the client, round-trips, and equals its `inspect_*`."""
    text = _read_text(f"inkscape://document/{doc_id}/{leaf}")
    json.loads(text)  # parses as JSON
    decoded = model.model_validate_json(text)
    assert decoded is not None
    # Parity contract (E1-05): the resource payload IS the matching inspect_document component.
    assert decoded == inspect_fn(doc_id)


def test_all_thirteen_resource_uris_covered(doc_id: str) -> None:
    """Guard: exactly the 13 resource URIs in scope read cleanly over the client (no error)."""
    uris = [uri for uri, _ in _STATIC_RESOURCES]
    uris += [f"inkscape://document/{doc_id}/{leaf}" for leaf, _, _ in _DOC_RESOURCES]
    assert len(uris) == 13
    for uri in uris:
        assert _read_text(uri)  # non-empty payload, no serialization error


# --- E11-10c: per-doc resource URIs discoverable via the listing the client tool reads ---------


def _listed_resource_uris() -> set[str]:
    """The concrete resource URIs the client sees — the same list `ListMcpResourcesTool` returns."""

    async def _run() -> set[str]:
        async with Client(mcp) as client:
            resources = await client.list_resources()
        return {str(r.uri) for r in resources}

    return asyncio.run(_run())


def test_documents_index_listed_and_readable_over_client(doc_id: str) -> None:
    """The `inkscape://documents` index is a concrete resource (listed, not a template) and reads.

    This is the discoverability path: `ListMcpResourcesTool` reads `resources/list`, which contains
    concrete resources only — the per-doc templates never appear there, so the index is how an agent
    discovers an opened document's concrete per-doc resource URIs.
    """
    assert "inkscape://documents" in _listed_resource_uris()

    payload = json.loads(_read_text("inkscape://documents"))
    entry = next(d for d in payload["documents"] if d["doc_id"] == doc_id)
    # Each listed per-doc URI actually resolves over the client.
    for uri in entry["resources"].values():
        assert _read_text(uri)
