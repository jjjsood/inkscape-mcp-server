"""End-to-end workflow harness: one real MCP client session per test, real Inkscape.

Each test drives the production app (`build_app()`) through a single `fastmcp.Client` the way an
agent would: many chained calls whose later steps depend on earlier results. Artifacts are checked
by content (lxml) and pixels (Pillow), not just "no error".
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from lxml import etree
from mcp.types import ImageContent, TextContent
from PIL import Image

FIXTURES = Path(__file__).parent / "fixtures"
SVG_NS = "http://www.w3.org/2000/svg"

pytestmark = pytest.mark.inkscape


@dataclass
class Result:
    data: dict[str, Any]
    images: list[ImageContent] = field(default_factory=list)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def image(self, n: int = 0) -> Image.Image:
        import base64

        return Image.open(io.BytesIO(base64.b64decode(self.images[n].data))).convert("RGBA")


@dataclass
class Session:
    """A live client session plus the workspace it is sandboxed to."""

    client: Client[Any]
    ws: Path
    calls: int = 0

    async def __call__(self, tool: str, **args: Any) -> Result:
        self.calls += 1
        res = await self.client.call_tool(tool, args)
        (text,) = [c.text for c in res.content if isinstance(c, TextContent)]
        return Result(json.loads(text), [c for c in res.content if isinstance(c, ImageContent)])

    async def fails(self, tool: str, **args: Any) -> str:
        """Call a tool that must be refused; returns the error message."""
        self.calls += 1
        with pytest.raises(ToolError) as exc:
            await self.client.call_tool(tool, args)
        return str(exc.value)

    def fixture(self, name: str, as_name: str | None = None) -> str:
        shutil.copy(FIXTURES / name, self.ws / (as_name or name))
        return as_name or name

    def file(self, rel: str) -> Path:
        return self.ws / rel

    def svg(self, rel: str) -> etree._Element:
        return etree.parse(str(self.ws / rel)).getroot()

    def png(self, rel: str) -> Image.Image:
        return Image.open(self.ws / rel).convert("RGBA")


@pytest.fixture
def run_flow(tool_ws: Path) -> Callable[[Callable[[Session], Awaitable[None]]], Session]:
    """Run an async workflow against one client session of the production app."""
    from inkscape_mcp.tools import build_app

    def run(flow: Callable[[Session], Awaitable[None]]) -> Session:
        async def go() -> Session:
            async with Client(build_app()) as client:
                s = Session(client, tool_ws)
                await flow(s)
                return s

        return asyncio.run(go())

    return run


def by_id(root: etree._Element, elem_id: str) -> etree._Element:
    (elem,) = root.xpath("//*[@id=$i]", i=elem_id)
    return elem


def text_of(root: etree._Element, elem_id: str) -> str:
    return "".join(by_id(root, elem_id).itertext())


def near(px: tuple[int, ...], hex_color: str, tol: int = 24) -> bool:
    """True when an RGBA pixel is within `tol` per channel of `#rrggbb`."""
    want = [int(hex_color[i : i + 2], 16) for i in (1, 3, 5)]
    return all(abs(a - b) <= tol for a, b in zip(px[:3], want, strict=True))


def pixel(img: Image.Image, x: float, y: float, page_w: float) -> tuple[int, ...]:
    """The pixel at page coordinate (x, y) of an image rendered from a page `page_w` wide."""
    k = img.width / page_w
    return img.getpixel((int(x * k), int(y * k)))  # type: ignore[return-value]
