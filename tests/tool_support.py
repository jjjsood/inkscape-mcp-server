"""Helpers for the result-oriented tool surface tests (`tools/`)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastmcp import Client, FastMCP
from mcp.types import ImageContent, TextContent

from inkscape_mcp.registry import get_registry
from inkscape_mcp.render.cli import RenderResult
from inkscape_mcp.workspace import sandbox

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
    b'<rect id="r1" x="0" y="0" width="10" height="10" fill="#ff0000"/>'
    b'<rect id="r2" x="20" y="0" width="10" height="10" fill="#ff0000"/>'
    b'<text id="t1" x="0" y="20">hello</text>'
    b"</svg>"
)

PNG = b"\x89PNG\r\n\x1a\n"


@dataclass
class Called:
    """A tool call's outcome: parsed JSON text and inline images."""

    data: Any
    images: list[ImageContent] = field(default_factory=list)


def call(app: FastMCP, name: str, args: dict[str, Any] | None = None) -> Called:
    """Call a tool through an in-memory client. A tool error raises `ToolError`."""

    async def go() -> Called:
        async with Client(app) as client:
            res = await client.call_tool(name, args or {})
            texts = [c for c in res.content if isinstance(c, TextContent)]
            images = [c for c in res.content if isinstance(c, ImageContent)]
            assert len(texts) == 1, "every tool returns exactly one text block"
            return Called(data=json.loads(texts[0].text), images=images)

    return asyncio.run(go())


def write_svg(ws: Path, name: str = "logo.svg", data: bytes = SVG) -> Path:
    path = ws / name
    path.write_bytes(data)
    return path


def open_doc(ws: Path, name: str = "logo.svg", data: bytes = SVG) -> str:
    return get_registry().open_document(str(write_svg(ws, name, data))).doc_id


def working_svg(doc_id: str) -> str:
    root = get_registry().get(doc_id).root
    return sandbox.working_copy(Path(root), doc_id).read_text(encoding="utf-8")


def fake_render(calls: list[str]) -> Any:
    """A stand-in for `render_preview` that writes a stub PNG and records the call."""

    def render(doc_id: str, width_px: int | None = None, **_: object) -> RenderResult:
        calls.append(doc_id)
        root = Path(get_registry().get(doc_id).root)
        out_dir = sandbox.artifacts_dir(root, doc_id) / "preview"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"preview-{len(calls)}.png"
        out.write_bytes(PNG)
        rel = out.relative_to(root).as_posix()
        return RenderResult(
            doc_id=doc_id,
            workspace_relative_path=rel,
            format="png",
            width_px=10,
            height_px=10,
            duration_s=0.0,
        )

    return render


def tool_schemas(app: FastMCP) -> dict[str, dict[str, Any]]:
    """`{tool name: input JSON schema}` for every registered tool."""
    return {t.name: t.parameters for t in asyncio.run(app.list_tools())}


def _enums(node: Any, key: str = "enum") -> set[str]:
    """Every string `enum` (or `const`) value anywhere inside a JSON-schema fragment."""
    found: set[str] = set()
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key == "enum":
                found |= {v for v in value if isinstance(v, str)}
            elif name == key == "const" and isinstance(value, str):
                found.add(value)
            else:
                found |= _enums(value, key)
    elif isinstance(node, list):
        for item in node:
            found |= _enums(item, key)
    return found


def target_ops(tool: str, schemas: dict[str, dict[str, Any]]) -> set[str]:
    """The `op` values a `(tool, op)` intent target may carry for `tool`, read off the schemas.

    `edit`: the op names; `open`: the source modes (its parameters beyond the call options it
    shares with `edit`); `export`: the target `kind` consts; everything else the enum of its
    `action` / `topic` / `target` / `include` parameter.
    """
    from inkscape_mcp.edit.op_docs import op_models

    props = schemas[tool].get("properties", {})
    if tool == "edit":
        return set(op_models())
    if tool == "open":
        return set(props) - set(schemas["edit"]["properties"])
    if tool == "export":
        return _enums(props["targets"], "const")
    ops: set[str] = set()
    for name in ("action", "topic", "target", "include"):
        ops |= _enums(props.get(name, {}))
    return ops
