#!/usr/bin/env python3
"""Workflow smoke for the ten-tool surface, over a REAL MCP client.

Plays the workflows WF1-WF8 from ``docs/test-scenarios.md`` against the ``inkscape-mcp`` server.
Each workflow is one long session the way an agent works through a task: look, change, look
again, take feedback, make mistakes, roll back, ship files, change them again. Every step's input
comes from earlier results, and the checks are on RESULTS: ids, snapshot counts, pixels, file
contents, originals untouched, error texts that name the next step. Single functions are covered
by the unit tests; this script covers how they play together.

By default the server is started as a subprocess and driven over STDIO, exactly like an MCP host
does. ``--in-process`` uses the FastMCP in-memory client instead (faster, same assertions).

Steps that need the Inkscape engine (``render``, file/profile ``export``, ``render='inline'``,
path ops) are skipped when no ``inkscape`` is on PATH; the DOM steps of the same workflow still
run and the workflow is reported as ``SKIP``. ``--require-inkscape`` turns that into ``FAIL``.

Path ops (WF6) are operator-gated, so the server runs with ``INKSCAPE_MCP_RAW_ACTION_ENABLED=1``.

Report-only: not part of CI and not collected by pytest.

Usage (run inside ``inkscape-mcp-server/``)::

    uv run python scripts/scenario_smoke.py                  # all workflows over STDIO
    uv run python scripts/scenario_smoke.py --list           # list workflows, call nothing
    uv run python scripts/scenario_smoke.py --only WF1,WF6   # a subset
    uv run python scripts/scenario_smoke.py --in-process     # in-memory client
    uv run python scripts/scenario_smoke.py --cmd "inkscape-mcp"   # another server command
    uv run python scripts/scenario_smoke.py --json --keep    # JSON report, keep the workspace

Exit 0 when no workflow FAILed (SKIPs are fine), 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

#: Fixture: red background, purple dot, text (dot must differ from bg, else it is invisible).
FIXTURE_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="120" viewBox="0 0 200 120">
  <rect id="bg" x="0" y="0" width="200" height="120" fill="#ff0000"/>
  <circle id="dot" cx="100" cy="60" r="30" fill="#6a0dad"/>
  <text id="label" x="20" y="100" fill="#222222">Hello</text>
</svg>
"""

#: Any non-empty token is accepted by the server (presence check only).
TOKEN = "scenario-smoke-approval"  # noqa: S105 (dummy approval token, not a secret)

REPO_ROOT = Path(__file__).resolve().parent.parent
POSTER = REPO_ROOT / "tests" / "e2e" / "fixtures" / "brand_poster.svg"

OLD_RED, OLD_NAVY, TEAL, NAVY = "#e63946", "#1d3557", "#2a9d8f", "#264653"
BG_RED, DOT_PURPLE = "#ff0000", "#6a0dad"  # logo.svg fixture: background and dot


class Failed(Exception):
    """A check did not hold."""


@dataclass
class Called:
    """One tool call: the parsed JSON text block and the inline PNGs."""

    data: dict[str, Any]
    pngs: list[bytes] = field(default_factory=list)

    @property
    def images(self) -> int:
        return len(self.pngs)

    def image(self) -> Image.Image:
        if not self.pngs:
            raise Failed("no inline image returned")
        return Image.open(io.BytesIO(self.pngs[0])).convert("RGBA")


@dataclass
class Ctx:
    """Per-workflow state: the client, the workspace, call and check counters."""

    client: Any
    ws: Path
    engine: bool
    require_engine: bool
    calls: int = 0
    checks: int = 0
    skipped: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    async def call(self, tool: str, args: dict[str, Any]) -> Called:
        """Call a tool that must succeed."""
        self.calls += 1
        try:
            res = await self.client.call_tool(tool, args)
        except Exception as exc:
            raise Failed(f"{tool} failed unexpectedly: {exc}") from exc
        out = Called({})
        for block in res.content or []:
            text = getattr(block, "text", None)
            if text:
                out.data = json.loads(text)
            elif getattr(block, "data", None):
                out.pngs.append(base64.b64decode(block.data))
        self.log.append(f"{tool} -> {sorted(out.data)} images={out.images}")
        return out

    async def fail(self, tool: str, args: dict[str, Any]) -> str:
        """Call a tool that must fail; return the error text."""
        from fastmcp.exceptions import ToolError

        self.calls += 1
        try:
            await self.client.call_tool(tool, args)
        except ToolError as exc:
            self.log.append(f"{tool} -> error: {exc}")
            return str(exc)
        raise Failed(f"{tool} {args} succeeded but was expected to fail")

    async def attempt(self, tool: str, args: dict[str, Any]) -> tuple[Called | None, str]:
        """Call a tool that may succeed or fail; return the result or the error text."""
        try:
            return await self.call(tool, args), ""
        except Failed as exc:
            return None, str(exc)

    def check(self, cond: bool, what: str) -> None:
        if not cond:
            raise Failed(what)
        self.checks += 1

    def has_engine(self, step: str) -> bool:
        """True when Inkscape is there; otherwise note the skipped step (or FAIL if required)."""
        if self.engine:
            return True
        if self.require_engine:
            raise Failed(f"{step} needs inkscape on PATH (--require-inkscape)")
        self.skipped.append(step)
        return False

    def inline(self) -> dict[str, Any]:
        """``render='inline'`` when the renderer exists; the edit itself runs either way."""
        return {"render": "inline"} if self.engine else {}

    # --- helpers shared by the workflows ------------------------------------------------------

    def fixture(self, name: str, text: str = FIXTURE_SVG) -> Path:
        path = self.ws / f"{name}.svg"
        path.write_text(text, encoding="utf-8")
        return path

    def poster(self, name: str) -> Path:
        return self.fixture(name, POSTER.read_text(encoding="utf-8"))

    async def open_path(self, path: Path, **extra: Any) -> Called:
        res = await self.call("open", {"path": str(path), **extra})
        self.check(bool(res.data.get("doc_id")), "open returned a doc_id")
        return res

    async def edit(self, doc_id: str, ops: list[dict[str, Any]], **extra: Any) -> Called:
        return await self.call("edit", {"doc_id": doc_id, "ops": ops, **extra})

    async def snapshot_count(self, doc_id: str) -> int:
        res = await self.call("history", {"doc_id": doc_id, "action": "list"})
        count = int(res.data.get("count", -1))
        self.check(count == len(res.data.get("snapshots", [])), "history count matches the list")
        return count

    async def objects(self, doc_id: str, select: dict[str, Any] | None = None) -> list[Any]:
        args: dict[str, Any] = {"doc_id": doc_id, "include": ["objects"], "detail": "full"}
        if select:
            args["select"] = {"max_matches": 1024, **select}
        args["limit"] = 1000
        res = await self.call("inspect", args)
        return list(res.data.get("objects", {}).get("objects", []))

    async def ids(self, doc_id: str, select: dict[str, Any] | None = None) -> set[str]:
        return ids_of(await self.objects(doc_id, select))

    async def row(self, doc_id: str, obj_id: str) -> dict[str, Any]:
        rows = [r for r in await self.objects(doc_id) if r.get("id") == obj_id]
        self.check(len(rows) == 1, f"object {obj_id!r} exists")
        return dict(rows[0])

    async def fingerprint(self, doc_id: str) -> str:
        return json.dumps(await self.objects(doc_id), sort_keys=True)

    async def save(self, doc_id: str, rel: str, **target: Any) -> Called:
        return await self.call(
            "export",
            {
                "doc_id": doc_id,
                "targets": [{"kind": "save_as", "dest_path": str(self.ws / rel), **target}],
                **({"approval_token": TOKEN} if target.get("overwrite") else {}),
            },
        )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ids_of(rows: list[Any]) -> set[str]:
    return {r["id"] for r in rows if isinstance(r, dict) and r.get("id")}


def centre(bbox: list[float]) -> tuple[float, float]:
    x, y, w, h = bbox
    return (round(x + w / 2, 3), round(y + h / 2, 3))


def near(px: tuple[int, ...], hex_color: str, tol: int = 30) -> bool:
    """True when an RGBA pixel is within ``tol`` per channel of ``#rrggbb``."""
    want = [int(hex_color[i : i + 2], 16) for i in (1, 3, 5)]
    return all(abs(a - b) <= tol for a, b in zip(px[:3], want, strict=True))


def pixel(img: Image.Image, x: float, y: float, page_w: float) -> tuple[int, ...]:
    """The pixel at page coordinate ``(x, y)`` of an image rendered from a page ``page_w`` wide."""
    k = img.width / page_w
    return img.getpixel((int(x * k), int(y * k)))  # type: ignore[no-any-return]


def grid_svg(n_cols: int = 12, n_rows: int = 10) -> str:
    """120 rects ``c{r}_{c}`` on a 40 px grid, red when ``(r+c) % 3 == 0`` else blue.

    The canvas is one row (40 px) taller than the grid: an empty strip at the bottom.
    """
    cells = "".join(
        f'<rect id="c{r}_{c}" x="{c * 40 + 5}" y="{r * 40 + 5}" width="30" height="30" '
        f'fill="{"#cc0000" if (r + c) % 3 == 0 else "#0055cc"}"/>'
        for r in range(n_rows)
        for c in range(n_cols)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{n_cols * 40}" '
        f'height="{n_rows * 40 + 40}" viewBox="0 0 {n_cols * 40} {n_rows * 40 + 40}">'
        f"{cells}</svg>"
    )


# --- workflows -------------------------------------------------------------------------------


async def wf1_logo_feedback_delivery(c: Ctx) -> None:
    """Logo from scratch, three feedback rounds, delivery, then a late fix of the delivered file."""
    res = await c.call(
        "open",
        {
            "canvas": {"width": 200, "height": 120, "background": "#ffffff"},
            "ops": [
                {"op": "create_rect", "x": 0, "y": 0, "width": 200, "height": 120,
                 "fill": "#3366cc", "id": "bg"},
                {"op": "create_circle", "cx": 100, "cy": 50, "r": 25, "fill": "#ffffff",
                 "id": "mark"},
                {"op": "create_text", "x": 100, "y": 105, "text": "Logo", "fill": "#ffffff",
                 "anchor": "middle", "id": "name"},
                {"op": "group", "ids": ["$1", "$2"], "id": "brand"},
            ],
            **c.inline(),
        },
    )  # fmt: skip
    doc = res.data["doc_id"]
    created = {e["op"]: e["id"] for e in res.data.get("created", [])}
    c.check(created == {0: "bg", 1: "mark", 2: "name", 3: "brand"}, f"one id per op: {created}")
    if c.engine:
        img = res.image()
        c.check(near(pixel(img, 5, 5, 200), "#3366cc"), "background is blue")
        c.check(near(pixel(img, 100, 50, 200), "#ffffff"), "mark is white in the middle")
    snaps = await c.snapshot_count(doc)
    mark_centre = centre((await c.row(doc, "mark"))["bbox"])

    # Round 1: "mark bigger, but keep it centred".
    await c.edit(doc, [{"op": "scale", "ids": ["mark"], "sx": 1.3}], **c.inline())
    bbox = (await c.row(doc, "mark"))["bbox"]
    c.check(centre(bbox) == mark_centre, f"scaled about its own centre: {bbox}")
    c.check(abs(bbox[2] - 65) < 0.01, f"mark is 30% bigger: {bbox}")
    # Round 2: "background teal instead".
    r2 = await c.edit(doc, [{"op": "fill", "ids": ["bg"], "color": TEAL}], **c.inline())
    if c.engine:
        c.check(near(pixel(r2.image(), 5, 5, 200), TEAL), "background is teal in the picture")
    # Round 3: "the name is Acme".
    await c.edit(doc, [{"op": "replace_text", "ids": ["name"], "text": "Acme"}])
    c.check("name" in await c.ids(doc, {"text": "acme"}), "text replaced")
    c.check(await c.snapshot_count(doc) == snaps + 3, "one snapshot per feedback round")

    if c.has_engine("object render"):
        obj = await c.call("render", {"doc_id": doc, "target": "object", "object_id": "mark"})
        mid = obj.image().width // 2
        c.check(near(obj.image().getpixel((mid, mid)), "#ffffff"), "object render: white mark")

    # Delivery: master SVG, then icon set and web profile.
    saved = await c.save(doc, "deliver/logo.svg")
    dest = c.ws / "deliver" / "logo.svg"
    c.check(saved.data["saved"]["path"] == "wf1/deliver/logo.svg", f"relative path: {saved.data}")
    c.check("Acme" in dest.read_text(encoding="utf-8"), "delivered SVG has the final text")
    if c.has_engine("icons + web profile export"):
        out = await c.call(
            "export",
            {
                "doc_id": doc,
                "targets": [
                    {"kind": "profile", "name": "icons"},
                    {"kind": "profile", "name": "web"},
                ],
                "name_prefix": "logo",
            },
        )
        arts = out.data.get("artifacts", [])
        c.check(len(arts) >= 3, f"icon ladder + web files: {arts}")
        for art in arts:
            path = str(art.get("path") or "")
            c.check(bool(path) and not Path(path).is_absolute(), f"workspace-relative: {path}")
            c.check((c.ws.parent / path).stat().st_size > 0, f"artifact on disk: {path}")

    # Late fix after delivery: overwrite needs overwrite=True AND a token.
    await c.edit(doc, [{"op": "fill", "ids": ["mark"], "color": "#f4a261"}])
    old = sha256(dest)
    msg = await c.fail("export", {"doc_id": doc, "targets": [
        {"kind": "save_as", "dest_path": str(dest)}]})  # fmt: skip
    c.check("overwrite=True and an approval_token" in msg, msg)
    msg = await c.fail("export", {"doc_id": doc, "targets": [
        {"kind": "save_as", "dest_path": str(dest), "overwrite": True}]})  # fmt: skip
    c.check("approval_token" in msg, msg)
    c.check(sha256(dest) == old, "refused saves left the delivered file alone")
    done = await c.save(doc, "deliver/logo.svg", overwrite=True)
    c.check(done.data["saved"].get("overwritten") is True, "overwritten flagged")
    c.check("#f4a261" in dest.read_text(encoding="utf-8"), "delivered file carries the fix")
    if c.has_engine("re-export after the late fix"):  # icons + web from turn 5 are stale now
        again = await c.call("export", {"doc_id": doc, "targets": [
            {"kind": "profile", "name": "icons"}, {"kind": "profile", "name": "web"}],
            "name_prefix": "logo"})  # fmt: skip
        svgs = [a for a in again.data.get("artifacts", []) if str(a.get("path")).endswith(".svg")]
        c.check(
            bool(svgs)
            and all("#f4a261" in (c.ws.parent / a["path"]).read_text("utf-8") for a in svgs),
            f"regenerated web SVG has the fix: {svgs}",
        )


async def wf2_brand_refresh(c: Ctx) -> None:
    """Rebrand a layered Inkscape poster (CSS classes, locked layer), keep it editable, ship it."""
    src = c.poster("poster")
    original = sha256(src)
    opened = await c.open_path(src, **c.inline())
    doc = opened.data["doc_id"]
    c.check(opened.data["summary"]["layers"] == 3, "three layers")

    info = await c.call("inspect", {"doc_id": doc, "include": ["layers", "styles"]})
    layers = info.data["layers"]["layers"]
    c.check([la["locked"] for la in layers] == [True, False, False], f"bg locked: {layers}")
    c.check(OLD_RED in info.data["styles"]["colors"], "old accent in use")
    accent = await c.ids(doc, {"fill": OLD_RED})
    c.check(accent == {"band", "burst", "cta", "logo-ring"}, f"CSS-painted objects found: {accent}")
    c.check("headline" in await c.ids(doc, {"text": "spring"}), "headline found by text")
    cta = (await c.row(doc, "cta"))["bbox"]

    # The whole rebrand in ONE call.
    res = await c.edit(
        doc,
        [
            {"op": "palette", "mapping": {OLD_RED: TEAL}},
            {"op": "replace_text", "ids": ["headline"], "text": "Summer Sale"},
            {"op": "scale", "ids": ["cta"], "sx": 1.2},
            {"op": "create_text", "x": 370, "y": 220, "text": "NEW", "fill": "#ffffff",
             "anchor": "end", "id": "new-tag"},
        ],
        detail="summary",
        **c.inline(),
    )  # fmt: skip
    c.check(len(res.data.get("ops", [])) == 4, "one summary line per op")
    if c.engine:
        img = res.image()
        c.check(near(pixel(img, 20, 290, 400), TEAL), "CSS .accent band recoloured")
        c.check(near(pixel(img, 330, 90, 400), TEAL), "CSS .accent burst recoloured")
    c.check(not await c.ids(doc, {"fill": OLD_RED}), "no old accent left")
    styles = await c.call("inspect", {"doc_id": doc, "include": ["styles"]})
    c.check(OLD_RED not in styles.data["styles"]["colors"], "old accent gone from CSS too")
    cta2 = (await c.row(doc, "cta"))["bbox"]
    c.check(centre(cta2) == centre(cta), f"cta stays in place: {cta} -> {cta2}")
    c.check(abs(cta2[2] - cta[2] * 1.2) < 0.5, f"cta 20% wider: {cta2}")

    # Web-ready but still editable: the default optimize keeps layers and the lock.
    await c.edit(doc, [{"op": "optimize_web"}])
    after = (await c.call("inspect", {"doc_id": doc, "include": ["layers"]})).data["layers"]
    c.check([la["locked"] for la in after["layers"]] == [True, False, False], "layers kept")
    await c.save(doc, "poster-v2.svg")
    v2 = (c.ws / "poster-v2.svg").read_text(encoding="utf-8")
    c.check("Summer Sale" in v2 and TEAL in v2 and OLD_RED not in v2, "v2 rebranded")
    c.check(v2.count('groupmode="layer"') == 3, "v2 keeps its layers")
    content = v2.split('id="layer-content"', 1)[1].split('groupmode="layer"', 1)[0]
    c.check('id="new-tag"' in content, "NEW tag landed in the Content layer, not the locked one")
    if c.has_engine("web profile export"):
        web = await c.call(
            "export", {"doc_id": doc, "targets": [{"kind": "profile", "name": "web"}]}
        )
        c.check(len(web.data.get("artifacts", [])) >= 2, "web profile wrote PNG + SVG")

    # Ship-only pass drops editor data: HIGH, refused without a token.
    msg = await c.fail("edit", {"doc_id": doc, "ops": [{"op": "optimize_web", "aggressive": True}]})
    c.check("approval_token" in msg, msg)
    await c.edit(doc, [{"op": "optimize_web", "aggressive": True}], approval_token=TOKEN)
    await c.save(doc, "poster-ship.svg")
    c.check("sodipodi:namedview" not in (c.ws / "poster-ship.svg").read_text(encoding="utf-8"),
            "ship copy has no editor cruft")  # fmt: skip
    c.check(sha256(src) == original, "source poster never touched")


async def wf3_mistakes_and_recovery(c: Ctx) -> None:
    """Wrong edits, atomic failure, no-op, refused delete, undo, outside change, reload, prune."""
    src = c.poster("poster")
    doc = (await c.open_path(src)).data["doc_id"]
    start = await c.fingerprint(doc)
    good = await c.call("history", {"doc_id": doc, "action": "create", "label": "good"})

    # Mistake 1: moved the wrong group off the page -> back to the checkpoint.
    await c.edit(doc, [{"op": "move", "ids": ["header"], "dx": 900}])
    back = await c.call(
        "history", {"doc_id": doc, "action": "restore", "snapshot_id": good.data["snapshot_id"]}
    )
    c.check(bool(back.data.get("pre_restore_snapshot_id")), "restore is itself reversible")
    c.check(await c.fingerprint(doc) == start, "checkpoint restored exactly")

    # Mistake 2: a typo in a long call -> nothing applied, error names the op and a suggestion.
    snaps = await c.snapshot_count(doc)
    call: list[dict[str, Any]] = [
        {"op": "create_rect", "x": 300, "y": 180, "width": 80, "height": 36, "fill": "#ffffff",
         "id": "badge"},
        {"op": "replace_text", "ids": ["headline"], "text": "Final Sale"},
        {"op": "fill", "ids": ["brand-nam"], "color": OLD_NAVY},
    ]  # fmt: skip
    msg = await c.fail("edit", {"doc_id": doc, "ops": call})
    c.check(msg.startswith("ops[2] fill:") and "brand-name" in msg, f"suggests the id: {msg}")
    c.check(await c.fingerprint(doc) == start and await c.snapshot_count(doc) == snaps, "no-op")
    call[2] = {"op": "fill", "ids": ["brand-name"], "color": OLD_NAVY}
    await c.edit(doc, call)
    c.check(await c.snapshot_count(doc) == snaps + 1, "corrected call = one snapshot")
    c.check("badge" in await c.ids(doc), "badge exists after the retry")

    # Asking for what is already true is a no-op, not an error.
    noop = await c.edit(doc, [{"op": "fill", "ids": ["brand-name"], "color": OLD_NAVY}])
    c.check(noop.data.get("changed") is False and not noop.data.get("snapshot_id"), "no-op")
    c.check(await c.snapshot_count(doc) == snaps + 1, "no-op wrote nothing")

    # Delete: refused, planned, approved, undone.
    msg = await c.fail("edit", {"doc_id": doc, "ops": [{"op": "delete", "ids": ["burst"]}]})
    c.check("delete is HIGH risk; re-call with approval_token" in msg, msg)
    plan = await c.edit(doc, [{"op": "delete", "select": {"tag": "path"}}], dry_run=True)
    c.check(plan.data.get("needs_approval") is True and plan.data.get("risk") == "high", "plan")
    c.check("burst" in plan.data["plan"][0]["matched"], f"plan names burst: {plan.data}")
    gone = await c.edit(doc, [{"op": "delete", "ids": ["burst"]}], approval_token=TOKEN)
    c.check(gone.data["snapshot_id"] in str(gone.data.get("undo")), "result names its undo")
    c.check("burst" not in await c.ids(doc), "burst deleted")
    await c.call(
        "history", {"doc_id": doc, "action": "restore", "snapshot_id": gone.data["snapshot_id"]}
    )
    now = await c.ids(doc)
    c.check({"burst", "badge"} <= now, "undo brings burst back and keeps the earlier fix")

    # Someone edits the source file; reload picks it up, and is itself reversible.
    src.write_text(src.read_text(encoding="utf-8").replace("Spring Sale", "Autumn Sale"), "utf-8")
    reloaded = await c.call("history", {"doc_id": doc, "action": "reload"})
    c.check("headline" in await c.ids(doc, {"text": "autumn"}), "reload re-read the source")
    c.check("badge" not in await c.ids(doc), "reload dropped the working-copy edits")
    await c.call("history", {"doc_id": doc, "action": "restore",
                             "snapshot_id": reloaded.data["pre_reload_snapshot_id"]})  # fmt: skip
    c.check("badge" in await c.ids(doc) and "headline" in await c.ids(doc, {"text": "final"}),
            "restore before the reload brings the edits back")  # fmt: skip

    msg = await c.fail("history", {"doc_id": doc, "action": "restore"})
    c.check("history(action='list')" in msg, msg)
    pruned = await c.call("history", {"doc_id": doc, "action": "prune"})
    c.check(pruned.data.get("retained", 0) >= 1, f"prune keeps snapshots: {pruned.data}")
    final = await c.call("inspect", {"doc_id": doc, "include": ["validate"]})
    c.check(final.data["validate"]["ok"] is True, "document still valid")


async def wf4_variants_contact_sheet(c: Ctx) -> None:
    """Three variants, compare them, compose a sheet, change a variant, rebuild, export the set."""
    docs = [(await c.open_path(c.poster(f"poster-{v}"))).data["doc_id"] for v in "abc"]
    a, b, cc = docs
    await c.edit(b, [{"op": "replace_text", "ids": ["headline"], "text": "Sale B"},
                     {"op": "palette", "mapping": {OLD_RED: TEAL}}])  # fmt: skip
    await c.edit(cc, [{"op": "replace_text", "ids": ["headline"], "text": "Sale C"},
                      {"op": "palette", "mapping": {OLD_RED: "#e9c46a"}}])  # fmt: skip
    multi = await c.call("inspect", {"doc_id": docs, "include": ["quality", "validate"]})
    c.check(set(multi.data.get("docs", {})) == set(docs), "one entry per variant")
    c.check("consistency" in multi.data, "set verdict across variants")
    c.check(bool(await c.ids(a, {"fill": OLD_RED})), "A keeps the original accent")
    c.check(not await c.ids(b, {"fill": OLD_RED}) and not await c.ids(cc, {"fill": OLD_RED}),
            "B and C have no old accent left (CSS included)")  # fmt: skip

    grid = {"op": "grid", "doc_ids": docs, "rows": 1, "cols": 3, "cell": 400, "gap": 20}
    canvas = {"width": 1240, "height": 300, "viewBox": "0 0 1240 300", "background": "#ffffff"}
    sheet = await c.call("open", {"canvas": canvas, "ops": [grid], **c.inline()})
    sid = sheet.data["doc_id"]
    c.check(bool(await c.ids(sid, {"text": "sale b"})), "variant B is on the sheet")
    if c.engine:
        img = sheet.image()
        c.check(near(pixel(img, 20, 250, 1240), OLD_RED) and near(pixel(img, 440, 250, 1240), TEAL),
                "each variant keeps its own CSS accent on the sheet")  # fmt: skip

    # The client changes B after the sheet exists: the sheet holds a copy, so rebuild it.
    await c.edit(b, [{"op": "replace_text", "ids": ["headline"], "text": "Sale B2"}])
    c.check(not await c.ids(sid, {"text": "sale b2"}), "sheet is a copy, not a live link")
    sheet2 = (await c.call("open", {"canvas": canvas, "ops": [grid]})).data["doc_id"]
    c.check(bool(await c.ids(sheet2, {"text": "sale b2"})), "rebuilt sheet shows the change")

    msg = await c.fail("export", {"doc_id": docs, "targets": [
        {"kind": "save_as", "dest_path": str(c.ws / "x.svg")}]})  # fmt: skip
    c.check("call export once per doc_id" in msg, msg)
    for d, name in zip(docs, "abc", strict=True):  # the error's advice: one call per doc_id
        await c.save(d, f"variants/poster-{name}.svg")
        c.check((c.ws / f"variants/poster-{name}.svg").is_file(), f"variant {name} saved")
    await c.save(sheet2, "sheets/contact.svg")
    c.check("Sale B2" in (c.ws / "sheets/contact.svg").read_text(encoding="utf-8"), "sheet saved")
    if c.has_engine("set export + sheet PDF"):
        batch = await c.call("export", {"doc_id": docs, "targets": [
            {"kind": "file", "format": "png", "width_px": 400}]})  # fmt: skip
        names = [Path(str(x["path"])).name for x in batch.data.get("artifacts", [])]
        c.check(len(names) == 3, f"one PNG per variant: {names}")
        c.check(all(any(f"-{d}-" in n for n in names) for d in docs), f"doc_id in names: {names}")
        pdf = await c.call("export", {"doc_id": sheet2, "targets": [
            {"kind": "file", "format": "pdf"}]})  # fmt: skip
        (art,) = pdf.data["artifacts"]
        c.check((c.ws.parent / art["path"]).read_bytes().startswith(b"%PDF"), "sheet PDF")


async def wf5_bulk_selectors(c: Ctx) -> None:
    """120 rects edited purely by selectors: counts, max_matches, max risk, bulk delete, budget."""
    doc = (await c.call("open", {"svg": grid_svg()})).data["doc_id"]
    total = len(await c.objects(doc))
    c.check(total == 120, f"120 rects: {total}")
    snaps = await c.snapshot_count(doc)
    red, quad = "#cc0000", [0, 0, 240, 200]
    ops: list[dict[str, Any]] = [
        {"op": "fill", "select": {"fill": red, "bbox": quad}, "color": "#ffcc00"},
        {"op": "fill", "select": {"fill": red}, "color": TEAL},
        {"op": "move", "select": {"fill": "#0055cc"}, "dx": 2},
    ]
    msg = await c.fail("edit", {"doc_id": doc, "ops": ops})
    c.check(msg.startswith("ops[2]") and "max_matches" in msg, f"too broad selector: {msg}")
    ops[2]["select"]["max_matches"] = 200
    done = await c.edit(doc, ops, detail="full")
    counts = [len(o.get("matched") or []) for o in done.data["outcomes"]]
    c.check(counts == [10, 30, 80], f"matches per op: {counts}")
    c.check(not await c.ids(doc, {"fill": red}) and await c.snapshot_count(doc) == snaps + 1,
            "no red left, one snapshot for the whole call")  # fmt: skip

    # One delete among ten creates makes the whole call HIGH: nothing created without a token.
    creates = [{"op": "create_rect", "x": i * 14 + 5, "y": 410, "width": 8, "height": 8,
                "fill": "#00aa00"} for i in range(10)]  # fmt: skip
    mixed: list[dict[str, Any]] = [*creates, {"op": "delete", "ids": ["c0_1"]}]
    msg = await c.fail("edit", {"doc_id": doc, "ops": mixed})
    c.check(msg.startswith("ops[10] delete is HIGH risk"), msg)
    c.check(len(await c.objects(doc)) == total, "none of the ten rects exists")
    c.check((await c.edit(doc, mixed, dry_run=True)).data.get("risk") == "high", "max risk")
    low = await c.edit(doc, creates)
    c.check(low.data.get("risk") == "medium" and len(low.data["created"]) == 10, "creates only")

    # Bulk delete after a preview of the count.
    sel = {"op": "delete", "select": {"fill": TEAL, "max_matches": 200}}
    plan = await c.edit(doc, [sel], dry_run=True)
    c.check(len(plan.data["plan"][0]["matched"]) == 30, f"plan: {plan.data['plan']}")
    await c.edit(doc, [sel], approval_token=TOKEN)
    c.check(len(await c.objects(doc)) == total + 10 - 30, "30 deleted")

    budget = {"doc_id": doc, "targets": [{"kind": "file", "format": "png", "width_px": 4000}],
              "byte_budget": 1000}  # fmt: skip
    dry = await c.call("export", {**budget, "dry_run": True})
    c.check(dry.data.get("within_budget") is False and "artifacts" not in dry.data, "over budget")
    if c.has_engine("page render"):
        img = (await c.call("render", {"doc_id": doc, "width_px": 480})).image()
        c.check(near(pixel(img, 22, 20, 480), "#ffcc00"), "c0_0 yellow")
        c.check(near(pixel(img, 62, 20, 480), "#0055cc"), "c0_1 blue, moved")
        c.check(pixel(img, 420, 340, 480)[3] < 40, "c8_10 deleted")


async def wf6_path_ops(c: Ctx) -> None:
    """Icon from primitives via union, difference, stroke_to_path, combine, break_apart + undo."""
    res = await c.call(
        "open",
        {
            "canvas": {"width": 64, "height": 64},
            "ops": [
                {"op": "create_rect", "x": 8, "y": 8, "width": 48, "height": 48,
                 "fill": NAVY, "id": "plate"},
                {"op": "create_circle", "cx": 32, "cy": 8, "r": 8, "fill": NAVY, "id": "knob"},
                {"op": "create_circle", "cx": 32, "cy": 32, "r": 10, "fill": "#000000",
                 "id": "hole"},
                {"op": "create_circle", "cx": 32, "cy": 32, "r": 28, "fill": "none",
                 "stroke": "#f4a261", "stroke_width": "3", "id": "ring"},
            ],
        },
    )  # fmt: skip
    doc = res.data["doc_id"]
    union = [{"op": "union", "ids": ["plate", "knob"]}]
    msg = await c.fail("edit", {"doc_id": doc, "ops": union})
    c.check("union is HIGH risk; re-call with approval_token" in msg, msg)
    plan = await c.edit(doc, union, dry_run=True)
    c.check(plan.data.get("risk") == "high" and plan.data.get("needs_approval") is True, "plan")
    c.check(plan.data["plan"][0]["matched"] == ["plate", "knob"], f"plan: {plan.data}")
    if not c.has_engine("path ops"):
        return

    await c.edit(doc, union, approval_token=TOKEN)
    c.check(await c.ids(doc) == {"plate", "hole", "ring"}, "union merged knob into plate")
    c.check((await c.row(doc, "plate"))["tag"] == "path", "union result is a path")
    cut = await c.edit(doc, [{"op": "difference", "ids": ["plate", "hole"]}],
                       approval_token=TOKEN, render="inline")  # fmt: skip
    img = cut.image()
    c.check(pixel(img, 32, 32, 64)[3] < 40, "difference punched a transparent hole")
    c.check(near(pixel(img, 14, 32, 64), NAVY), "plate still navy beside the hole")
    await c.edit(doc, [{"op": "stroke_to_path", "ids": ["ring"]},
                       {"op": "simplify", "ids": ["ring"]}], approval_token=TOKEN)  # fmt: skip
    ring = await c.row(doc, "ring")
    c.check(ring["tag"] == "path" and ring.get("fill") == "#f4a261", f"stroke became fill: {ring}")

    snaps = await c.snapshot_count(doc)
    await c.edit(doc, [{"op": "combine", "ids": ["plate", "ring"]}], approval_token=TOKEN)
    c.check(len(await c.objects(doc)) == 1, "combine made one compound path")
    apart = await c.edit(doc, [{"op": "break_apart", "select": {"tag": "path"}}],
                         approval_token=TOKEN)  # fmt: skip
    pieces = len(await c.objects(doc))
    c.check(pieces >= 3, f"break_apart split the compound path: {pieces}")
    c.check(
        len(apart.data["created"]) == pieces,
        f"break_apart reports every piece: {apart.data['created']}",
    )
    # Iteration: "no, keep it combined" -> undo only the last step.
    await c.call("history", {"doc_id": doc, "action": "restore",
                             "snapshot_id": apart.data["snapshot_id"]})  # fmt: skip
    c.check(len(await c.objects(doc)) == 1, "undo of break_apart keeps the combine")
    c.check(await c.snapshot_count(doc) == snaps + 3, "combine + break_apart + restore snapshots")

    out = await c.call("export", {"doc_id": doc, "targets": [
        {"kind": "file", "format": "png", "width_px": 128},
        {"kind": "save_as", "dest_path": str(c.ws / "icon-paths.svg")}]})  # fmt: skip
    c.check(len(out.data.get("artifacts", [])) >= 1, "PNG exported")
    svg = (c.ws / "icon-paths.svg").read_text(encoding="utf-8")
    c.check("<path" in svg and "<circle" not in svg and "<rect" not in svg, "only paths left")


async def wf7_foreign_svg_security(c: Ctx) -> None:
    """Graft and replace foreign SVG: HIGH risk, scrubbed, sandboxed paths, all reversible."""
    doc = (await c.open_path(c.fixture("logo"))).data["doc_id"]
    start = await c.fingerprint(doc)
    fragment = (
        '<g xmlns="http://www.w3.org/2000/svg" id="badge">'
        '<circle id="ins" cx="20" cy="20" r="8" fill="#00aa00" onload="alert(1)"/>'
        "<script>alert(2)</script></g>"
    )
    insert = [{"op": "insert_svg", "svg": fragment}]
    msg = await c.fail("edit", {"doc_id": doc, "ops": insert})
    c.check("disallowed event-handler attribute: 'onload'" in msg, msg)  # scrubbed before a token
    _, err = await c.attempt("edit", {"doc_id": doc, "ops": insert, "approval_token": TOKEN})
    if not err:
        c.check("ins" in await c.ids(doc), "safe part of the fragment inserted")

    # Mid-flow the agent tries a path outside the sandbox.
    for bad in ("../../etc/passwd", str(REPO_ROOT / "README.md")):
        msg = await c.fail("open", {"path": bad})
        c.check(msg.startswith("path rejected:") and "/etc" not in msg and str(REPO_ROOT)
                not in msg, f"no host path leaked: {msg}")  # fmt: skip

    # An entity-expanding replacement must not read host files.
    xxe = ('<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
           '<svg xmlns="http://www.w3.org/2000/svg"><text id="t">&x;</text></svg>')  # fmt: skip
    await c.attempt("edit", {"doc_id": doc, "ops": [{"op": "set_svg", "svg": xxe}],
                             "approval_token": TOKEN})  # fmt: skip
    await c.save(doc, "probe.svg")
    c.check("root:" not in (c.ws / "probe.svg").read_text(encoding="utf-8"), "no entity expansion")

    replace = [{"op": "set_svg", "svg": '<svg xmlns="http://www.w3.org/2000/svg" width="10" '
                'height="10"><rect id="only" width="10" height="10"/></svg>'}]  # fmt: skip
    swap = await c.edit(doc, replace, approval_token=TOKEN)
    c.check(await c.ids(doc) == {"only"}, "document replaced")
    await c.call("history", {"doc_id": doc, "action": "restore",
                             "snapshot_id": swap.data["snapshot_id"]})  # fmt: skip
    c.check({"bg", "dot", "label"} <= await c.ids(doc), "restore undid the replacement")
    await c.save(doc, "foreign.svg")
    text = (c.ws / "foreign.svg").read_text(encoding="utf-8")
    c.check("<script" not in text and "onload" not in text, f"scrubbed output {err}")
    c.check(err != "" or start != await c.fingerprint(doc), "fragment either refused or added")


async def wf8_long_iteration(c: Ctx) -> None:
    """Twelve edit rounds on one doc, deep restore, continue, prune: history stays exact."""
    doc = (await c.open_path(c.fixture("loop"))).data["doc_id"]
    base = await c.snapshot_count(doc)
    rounds: list[str] = []
    for i in range(1, 13):
        res = await c.edit(doc, [
            {"op": "move", "ids": ["dot"], "dx": 5},
            {"op": "create_rect", "x": i * 14, "y": 2, "width": 10, "height": 10,
             "fill": "#00aa00", "id": f"step{i}"},
        ])  # fmt: skip
        rounds.append(res.data["snapshot_id"])
        c.check(await c.snapshot_count(doc) == base + i, f"round {i}: exactly one snapshot")
        if i % 4 == 0 and c.has_engine(f"render round {i}"):
            img = (await c.call("render", {"doc_id": doc, "width_px": 200})).image()
            # Dot spans x 70+5i .. 130+5i at y=60: purple at its centre, background just left of it.
            c.check(near(pixel(img, 100 + 5 * i, 60, 200), DOT_PURPLE), f"dot moved in round {i}")
            c.check(near(pixel(img, 66 + 5 * i, 60, 200), BG_RED), f"old spot is background, {i}")

    # "Round 3 was the good one": the snapshot of round 4 holds the state after round 3.
    await c.call("history", {"doc_id": doc, "action": "restore", "snapshot_id": rounds[3]})
    now = await c.ids(doc)
    c.check({"step1", "step2", "step3"} <= now and "step4" not in now, f"state after 3: {now}")
    c.check(centre((await c.row(doc, "dot"))["bbox"])[0] == 115, "dot moved exactly 3x5")
    for i in (13, 14):
        await c.edit(doc, [{"op": "move", "ids": ["dot"], "dx": 5},
                           {"op": "create_rect", "x": 0, "y": i * 4, "width": 4, "height": 4,
                            "id": f"step{i}"}])  # fmt: skip
    now = await c.ids(doc)
    c.check({"step13", "step14"} <= now and "step12" not in now, "work continues after restore")
    c.check(centre((await c.row(doc, "dot"))["bbox"])[0] == 125, "dot moved 5x5 after restore")

    pruned = await c.call("history", {"doc_id": doc, "action": "prune"})
    c.check(pruned.data.get("retained", 0) >= 1, f"prune keeps snapshots: {pruned.data}")
    c.check({"bg", "label", "dot"} <= await c.ids(doc), "original ids stable through it all")
    final = await c.call("inspect", {"doc_id": doc, "include": ["validate"]})
    c.check(final.data["validate"]["ok"] is True, "document valid after 16 edits")


# --- runner ----------------------------------------------------------------------------------

Scenario = Callable[[Ctx], Awaitable[None]]

SCENARIOS: dict[str, Scenario] = {
    "WF1": wf1_logo_feedback_delivery,
    "WF2": wf2_brand_refresh,
    "WF3": wf3_mistakes_and_recovery,
    "WF4": wf4_variants_contact_sheet,
    "WF5": wf5_bulk_selectors,
    "WF6": wf6_path_ops,
    "WF7": wf7_foreign_svg_security,
    "WF8": wf8_long_iteration,
}


def title(fn: Scenario) -> str:
    return (fn.__doc__ or "").strip().splitlines()[0]


def server_env(ws: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["INKSCAPE_MCP_WORKSPACE_ROOTS"] = str(ws)
    # Live stays off; path ops (WF6) are behind the operator gate.
    env["INKSCAPE_MCP_LIVE_ENABLED"] = "0"
    env["INKSCAPE_MCP_RAW_ACTION_ENABLED"] = "1"
    return env


def make_client(args: argparse.Namespace, ws: Path) -> Any:
    from fastmcp import Client

    env = server_env(ws)
    if args.in_process:
        os.environ.update(env)
        from inkscape_mcp.server import mcp, register_tools

        register_tools()
        return Client(mcp)

    from fastmcp.client.transports import StdioTransport

    argv = shlex.split(args.cmd)
    transport = StdioTransport(
        command=argv[0], args=argv[1:], env=env, cwd=str(REPO_ROOT), keep_alive=False
    )
    return Client(transport, timeout=args.timeout)


async def run(args: argparse.Namespace, ws: Path, keys: list[str]) -> list[dict[str, Any]]:
    engine = shutil.which("inkscape") is not None
    report: list[dict[str, Any]] = []
    async with make_client(args, ws) as client:
        tools = sorted(t.name for t in await client.list_tools())
        print(f"server tools ({len(tools)}): {', '.join(tools)}", flush=True)
        for key in keys:
            wf_ws = ws / key.lower()
            wf_ws.mkdir()
            ctx = Ctx(client, wf_ws, engine, args.require_inkscape)
            started = time.monotonic()
            status, note = "PASS", ""
            try:
                await SCENARIOS[key](ctx)
                if ctx.skipped:
                    status, note = "SKIP", "no inkscape, skipped: " + ", ".join(ctx.skipped)
            except Failed as exc:
                status, note = "FAIL", str(exc)
            except Exception as exc:
                status, note = "FAIL", f"{type(exc).__name__}: {exc}"
            row = {
                "scenario": key,
                "title": title(SCENARIOS[key]),
                "status": status,
                "calls": ctx.calls,
                "checks_passed": ctx.checks,
                "seconds": round(time.monotonic() - started, 2),
                "note": note,
            }
            if status == "FAIL" and args.verbose:
                row["trace"] = ctx.log
            report.append(row)
            if not args.json:
                line = f"{status:4}  {key:4} calls={ctx.calls:<3} {row['title']}"
                print(line + (f"\n      -> {note}" if note else ""), flush=True)
                if status == "FAIL" and args.verbose:
                    for step in ctx.log:
                        print(f"         {step}", flush=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--list", action="store_true", help="list workflows and exit")
    parser.add_argument("--only", help="comma-separated workflow ids, e.g. WF1,WF6")
    parser.add_argument("--cmd", default="uv run inkscape-mcp", help="server command (STDIO)")
    parser.add_argument("--in-process", action="store_true", help="use the in-memory client")
    parser.add_argument("--require-inkscape", action="store_true", help="SKIP becomes FAIL")
    parser.add_argument("--timeout", type=float, default=120.0, help="per-request timeout (s)")
    parser.add_argument("--keep", action="store_true", help="keep the temp workspace")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="print call traces on FAIL")
    args = parser.parse_args()

    if args.list:
        for key, fn in SCENARIOS.items():
            print(f"{key:4} {title(fn)}")
        return 0

    keys = [k.strip().upper() for k in args.only.split(",")] if args.only else list(SCENARIOS)
    unknown = [k for k in keys if k not in SCENARIOS]
    if unknown:
        parser.error(f"unknown workflow(s): {', '.join(unknown)}; see --list")

    ws = Path(tempfile.mkdtemp(prefix="scenario_smoke_"))
    try:
        report = asyncio.run(run(args, ws, keys))
    finally:
        if args.keep:
            print(f"workspace kept: {ws}", file=sys.stderr, flush=True)
        else:
            shutil.rmtree(ws, ignore_errors=True)

    counts = {s: sum(r["status"] == s for r in report) for s in ("PASS", "FAIL", "SKIP")}
    if args.json:
        print(json.dumps({"summary": counts, "scenarios": report}, indent=2))
    else:
        print(f"\nPASS {counts['PASS']}  FAIL {counts['FAIL']}  SKIP {counts['SKIP']}")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
