#!/usr/bin/env python3
"""Scenario smoke for the reworked ten-tool surface, over a REAL MCP client.

Plays the scenarios from ``docs/test-scenarios.md`` (core workflow K1-K13, risk and approval
R1-R5) against the ``inkscape-mcp`` server and checks the RESULTS, not just reachability: ids
created, snapshots taken (or not), originals untouched, error texts that name the next step.
This is the "live smoke test" the rework plan lists as open (``docs/rework-plan.md``).

By default the server is started as a subprocess and driven over STDIO, exactly like an MCP host
does. ``--in-process`` uses the FastMCP in-memory client instead (faster, same assertions).

Steps that need the Inkscape engine (``render``, file/profile ``export``, ``render='inline'``) are
reported as ``SKIP`` when no ``inkscape`` is on PATH; ``--require-inkscape`` turns that into
``FAIL``. Everything else is pure DOM and runs anywhere.

Report-only: not part of CI and not collected by pytest.

Usage (run inside ``inkscape-mcp-server/``)::

    uv run python scripts/scenario_smoke.py                  # all scenarios over STDIO
    uv run python scripts/scenario_smoke.py --list           # list scenarios, call nothing
    uv run python scripts/scenario_smoke.py --only K1,R1     # a subset
    uv run python scripts/scenario_smoke.py --in-process     # in-memory client
    uv run python scripts/scenario_smoke.py --cmd "inkscape-mcp"   # another server command
    uv run python scripts/scenario_smoke.py --json --keep    # JSON report, keep the workspace

Exit 0 when no scenario FAILed (SKIPs are fine), 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
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

#: Fixture: two red shapes and a text (same as ``evals/run_call_count.py::SAMPLE_SVG``).
FIXTURE_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="120" viewBox="0 0 200 120">
  <rect id="bg" x="0" y="0" width="200" height="120" fill="#ff0000"/>
  <circle id="dot" cx="100" cy="60" r="30" fill="#ff0000"/>
  <text id="label" x="20" y="100" fill="#222222">Hello</text>
</svg>
"""

#: Any non-empty token is accepted by the server; the real one is minted out of band.
TOKEN = "scenario-smoke-approval"  # noqa: S105 (dummy approval token, not a secret)

REPO_ROOT = Path(__file__).resolve().parent.parent


class Failed(Exception):
    """A check did not hold."""


class Skipped(Exception):
    """The rest of the scenario needs the Inkscape engine, which is not available."""


@dataclass
class Called:
    """One tool call: the parsed JSON text block and the number of inline images."""

    data: dict[str, Any]
    images: int


@dataclass
class Ctx:
    """Per-scenario state: the client, the workspace, call and check counters."""

    client: Any
    ws: Path
    engine: bool
    require_engine: bool
    calls: int = 0
    checks: int = 0
    log: list[str] = field(default_factory=list)

    async def call(self, tool: str, args: dict[str, Any]) -> Called:
        """Call a tool that must succeed."""
        self.calls += 1
        try:
            res = await self.client.call_tool(tool, args)
        except Exception as exc:
            raise Failed(f"{tool} failed unexpectedly: {exc}") from exc
        data: dict[str, Any] = {}
        images = 0
        for block in res.content or []:
            text = getattr(block, "text", None)
            if text:
                data = json.loads(text)
            elif getattr(block, "data", None):
                images += 1
        self.log.append(f"{tool} -> {sorted(data)} images={images}")
        return Called(data, images)

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

    def needs_engine(self, step: str) -> None:
        """Stop here (SKIP) when Inkscape is missing, unless it is required."""
        if self.engine:
            return
        if self.require_engine:
            raise Failed(f"{step} needs inkscape on PATH (--require-inkscape)")
        raise Skipped(f"{step} needs inkscape on PATH")

    # --- helpers shared by the scenarios ------------------------------------------------------

    def fixture(self, name: str) -> Path:
        path = self.ws / f"{name}.svg"
        path.write_text(FIXTURE_SVG, encoding="utf-8")
        return path

    async def open_fixture(self, name: str) -> str:
        res = await self.call("open", {"path": str(self.fixture(name))})
        doc_id = res.data.get("doc_id")
        self.check(bool(doc_id), "open returned a doc_id")
        return str(doc_id)

    async def snapshot_count(self, doc_id: str) -> int:
        res = await self.call("history", {"doc_id": doc_id, "action": "list"})
        count = int(res.data.get("count", -1))
        self.check(count == len(res.data.get("snapshots", [])), "history count matches the list")
        return count

    async def objects(self, doc_id: str, select: dict[str, Any] | None = None) -> list[Any]:
        args: dict[str, Any] = {"doc_id": doc_id, "include": ["objects"], "detail": "full"}
        if select:
            args["select"] = select
        res = await self.call("inspect", args)
        return list(res.data.get("objects", {}).get("objects", []))

    async def fingerprint(self, doc_id: str) -> str:
        rows = await self.objects(doc_id)
        return json.dumps(rows, sort_keys=True)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ids_of(rows: list[Any]) -> set[str]:
    return {r["id"] for r in rows if isinstance(r, dict) and r.get("id")}


def one_line(msg: str) -> bool:
    return "\n" not in msg and "pydantic.dev" not in msg


# --- K: core workflow ------------------------------------------------------------------------


async def k1_logo_from_scratch(c: Ctx) -> None:
    """open(canvas, ops, render=inline) -> export([png, svg]) in two calls."""
    c.needs_engine("render='inline' and export")
    res = await c.call(
        "open",
        {
            "canvas": {"width": 200, "height": 120, "background": "#ffffff"},
            "ops": [
                {
                    "op": "create_rect",
                    "x": 0,
                    "y": 0,
                    "width": 200,
                    "height": 120,
                    "fill": "#3366cc",
                    "id": "bg",
                },
                {"op": "create_circle", "cx": 100, "cy": 60, "r": 30, "fill": "#ffffff"},
                {"op": "create_text", "x": 100, "y": 100, "text": "Logo", "anchor": "middle"},
            ],
            "render": "inline",
        },
    )
    doc_id = res.data["doc_id"]
    created = res.data.get("created", [])
    c.check([e["op"] for e in created] == [0, 1, 2], f"created one id per op: {created}")
    c.check(created[0]["id"] == "bg", "explicit id 'bg' kept")
    c.check(res.data.get("changed") is True and bool(res.data.get("snapshot_id")), "snapshot")
    c.check(res.images == 1, "render='inline' returned one image")

    out = await c.call(
        "export",
        {
            "doc_id": doc_id,
            "targets": [
                {"kind": "file", "format": "png", "width_px": 400},
                {"kind": "file", "format": "svg"},
            ],
        },
    )
    arts = out.data.get("artifacts", [])
    c.check(len(arts) == 2, f"two artifacts: {arts}")
    for art in arts:
        path = str(art.get("path") or "")
        c.check(bool(path) and not Path(path).is_absolute(), f"workspace-relative path: {path}")
        c.check((art.get("bytes") or 0) > 0, f"artifact has bytes: {art}")
    c.check(c.calls == 2, f"two calls in total, got {c.calls}")


async def k2_recolor_and_web_export(c: Ctx) -> None:
    """open(path) -> edit([palette, optimize_web], render=inline) -> export(profile web)."""
    src = c.fixture("k2")
    before = sha256(src)
    doc_id = (await c.call("open", {"path": str(src)})).data["doc_id"]
    ops = [{"op": "palette", "mapping": {"#ff0000": "#3366cc"}}, {"op": "optimize_web"}]
    edit_args: dict[str, Any] = {"doc_id": doc_id, "ops": ops}
    if c.engine:  # the inline look needs the renderer; without it the edit still runs
        edit_args["render"] = "inline"
    res = await c.call("edit", edit_args)
    c.check(res.data.get("changed") is True, "palette + optimize changed the document")
    c.check(res.images == (1 if c.engine else 0), "render='inline' returned one image")
    c.check(not await c.objects(doc_id, {"fill": "#ff0000"}), "no red object left")
    c.check(sha256(src) == before, "the original file is untouched")

    c.needs_engine("export profile web")
    out = await c.call(
        "export", {"doc_id": doc_id, "targets": [{"kind": "profile", "name": "web"}]}
    )
    arts = out.data.get("artifacts", [])
    c.check(len(arts) >= 2, f"web profile wrote PNGs and an SVG: {arts}")
    c.check(sha256(src) == before, "the original file is still untouched after export")


async def k3_dollar_refs(c: Ctx) -> None:
    """Create, group and move in one edit via "$n" references."""
    doc_id = await c.open_fixture("k3")
    snaps = await c.snapshot_count(doc_id)
    res = await c.call(
        "edit",
        {
            "doc_id": doc_id,
            "ops": [
                {
                    "op": "create_rect",
                    "x": 10,
                    "y": 10,
                    "width": 40,
                    "height": 20,
                    "fill": "#00aa00",
                },
                {"op": "create_text", "x": 12, "y": 25, "text": "Badge", "fill": "#ffffff"},
                {"op": "group", "ids": ["$0", "$1"], "id": "badge"},
                {"op": "move", "ids": ["$2"], "dx": 50, "dy": 30},
            ],
            "detail": "summary",
        },
    )
    created = {e["op"]: e["id"] for e in res.data.get("created", [])}
    c.check({0, 1, 2} <= set(created), f"ops 0-2 created ids: {created}")
    c.check(created.get(2) == "badge", "group got the explicit id")
    c.check(len(res.data.get("ops", [])) == 4, "detail=summary has one line per op")
    c.check(await c.snapshot_count(doc_id) == snaps + 1, "exactly one snapshot for the call")
    c.check("badge" in ids_of(await c.objects(doc_id, {"id_prefix": "badge"})), "group exists")


async def k4_selector_instead_of_lookup(c: Ctx) -> None:
    """A selector matches existing objects and ones created earlier in the same call."""
    doc_id = await c.open_fixture("k4")
    await c.call(
        "edit",
        {
            "doc_id": doc_id,
            "ops": [
                {
                    "op": "create_circle",
                    "cx": 20,
                    "cy": 20,
                    "r": 10,
                    "fill": "#ff0000",
                    "id": "new",
                },
                {"op": "fill", "select": {"fill": "#ff0000"}, "color": "#0000ff"},
            ],
        },
    )
    c.check(not await c.objects(doc_id, {"fill": "#ff0000"}), "nothing red left")
    blue = ids_of(await c.objects(doc_id, {"fill": "#0000ff"}))
    c.check({"bg", "dot", "new"} <= blue, f"bg, dot and the new circle are blue: {blue}")
    c.check("label" not in blue, "the dark text was not matched")


async def k5_atomic_rollback(c: Ctx) -> None:
    """One bad op rejects the whole call; nothing changes, no snapshot."""
    doc_id = await c.open_fixture("k5")
    before, snaps = await c.fingerprint(doc_id), await c.snapshot_count(doc_id)
    msg = await c.fail(
        "edit",
        {
            "doc_id": doc_id,
            "ops": [
                {"op": "create_rect", "x": 0, "y": 0, "width": 5, "height": 5, "id": "extra"},
                {"op": "fill", "ids": ["logo2"], "color": "#00ff00"},
            ],
        },
    )
    c.check(msg.startswith("ops[1] fill:") and "'logo2'" in msg and "not found" in msg, msg)
    c.check(await c.fingerprint(doc_id) == before, "document unchanged (op 0 rolled back)")
    c.check(await c.snapshot_count(doc_id) == snaps, "no snapshot for a rejected call")
    bad = await c.fail("edit", {"doc_id": doc_id, "ops": [{"op": "paint", "ids": ["bg"]}]})
    c.check(bad.startswith("invalid ops: ops[0]:") and "'create_rect'" in bad, f"lists ops: {bad}")


async def k6_dry_run_and_detail(c: Ctx) -> None:
    """dry_run plans without changing; detail=summary|full grow the result."""
    doc_id = await c.open_fixture("k6")
    before, snaps = await c.fingerprint(doc_id), await c.snapshot_count(doc_id)
    plan = await c.call(
        "edit",
        {
            "doc_id": doc_id,
            "ops": [{"op": "fill", "select": {"tag": "rect"}, "color": "#00ff00"}],
            "dry_run": True,
        },
    )
    c.check(plan.data.get("dry_run") is True, "dry_run flagged")
    matched = plan.data.get("plan", [{}])[0].get("matched") or []
    c.check("bg" in matched, f"plan names the matched id: {plan.data.get('plan')}")
    c.check(await c.fingerprint(doc_id) == before, "dry_run changed nothing")
    c.check(await c.snapshot_count(doc_id) == snaps, "dry_run took no snapshot")

    summary = await c.call(
        "edit",
        {
            "doc_id": doc_id,
            "ops": [{"op": "fill", "ids": ["bg"], "color": "#00ff00"}],
            "detail": "summary",
        },
    )
    c.check(len(summary.data.get("ops", [])) == 1, "summary: one line per op")
    c.check("operation_id" not in summary.data, "summary has no operation_id")
    full = await c.call(
        "edit",
        {
            "doc_id": doc_id,
            "ops": [{"op": "opacity", "ids": ["bg"], "opacity": 0.5}],
            "detail": "full",
        },
    )
    c.check(bool(full.data.get("operation_id")) and "outcomes" in full.data, "full detail")
    default = await c.call(
        "edit", {"doc_id": doc_id, "ops": [{"op": "move", "ids": ["dot"], "dx": 5, "dy": 0}]}
    )
    c.check("ops" not in default.data and "outcomes" not in default.data, "ids detail is compact")


async def k7_noop(c: Ctx) -> None:
    """A no-op edit reports changed=false and writes no snapshot."""
    doc_id = await c.open_fixture("k7")
    snaps = await c.snapshot_count(doc_id)
    res = await c.call(
        "edit", {"doc_id": doc_id, "ops": [{"op": "fill", "ids": ["bg"], "color": "#ff0000"}]}
    )
    c.check(res.data.get("changed") is False, f"changed=false: {res.data}")
    c.check(not res.data.get("snapshot_id"), "no snapshot id")
    c.check(await c.snapshot_count(doc_id) == snaps, "no snapshot written")
    none = await c.call(
        "edit",
        {
            "doc_id": doc_id,
            "ops": [{"op": "replace_color", "from_color": "#abcdef", "to_color": "#000000"}],
        },
    )
    c.check(none.data.get("changed") is False, "replace_color matching nothing is a no-op")


async def k8_inspect(c: Ctx) -> None:
    """include sections, select, limit/truncated, several doc_ids."""
    doc_id = await c.open_fixture("k8")
    other = await c.open_fixture("k8b")
    res = await c.call(
        "inspect",
        {"doc_id": doc_id, "include": ["summary", "objects", "styles", "validate", "quality"]},
    )
    for key in ("summary", "objects", "styles", "validate", "quality"):
        c.check(key in res.data, f"section {key} present")
    rows = res.data["objects"]["objects"]
    c.check(res.data["objects"].get("columns") == ["id", "tag", "bbox", "fill"], "compact rows")
    c.check(all(isinstance(r, list) and len(r) == 4 for r in rows), f"rows are 4-tuples: {rows}")
    sel = await c.call("inspect", {"doc_id": doc_id, "select": {"tag": "rect"}})
    c.check(list(sel.data) == ["doc_id", "objects"], "select alone returns only objects")
    c.check([r[0] for r in sel.data["objects"]["objects"]] == ["bg"], "select by tag")
    cut = await c.call("inspect", {"doc_id": doc_id, "include": ["objects"], "limit": 1})
    c.check(cut.data["objects"].get("truncated") is True, "limit sets truncated=true")
    multi = await c.call("inspect", {"doc_id": [doc_id, other], "include": ["quality"]})
    c.check(set(multi.data.get("docs", {})) == {doc_id, other}, "one entry per doc_id")
    c.check("consistency" in multi.data, "set verdict for several docs")


async def k9_render(c: Ctx) -> None:
    """render page and one object; argument errors name the fix."""
    doc_id = await c.open_fixture("k9")
    msg = await c.fail("render", {"doc_id": doc_id, "target": "object"})
    c.check("requires object_id" in msg, msg)
    msg = await c.fail("render", {"doc_id": doc_id, "target": "object", "object_id": "nope"})
    c.check("inspect(include=['objects'])" in msg, msg)
    c.needs_engine("render")
    page = await c.call("render", {"doc_id": doc_id, "width_px": 300})
    c.check(page.images == 1 and page.data.get("all_blank") is False, "page image, not blank")
    obj = await c.call("render", {"doc_id": doc_id, "target": "object", "object_id": "dot"})
    c.check(obj.images == 1, "object image")
    opened = await c.call("open", {"path": str(c.fixture("k9b")), "render": "inline"})
    c.check(opened.images == 1, "open(render='inline') without ops shows the page")


async def k10_history(c: Ctx) -> None:
    """create -> edit -> list -> restore; reload after an external change; prune."""
    src = c.fixture("k10")
    doc_id = (await c.call("open", {"path": str(src)})).data["doc_id"]
    original = await c.fingerprint(doc_id)
    cp = await c.call("history", {"doc_id": doc_id, "action": "create", "label": "checkpoint"})
    checkpoint = cp.data.get("snapshot_id")
    c.check(bool(checkpoint), "checkpoint created")
    await c.call(
        "edit",
        {"doc_id": doc_id, "ops": [{"op": "delete", "ids": ["dot"]}], "approval_token": TOKEN},
    )
    c.check("dot" not in ids_of(await c.objects(doc_id)), "dot deleted")
    listed = await c.call("history", {"doc_id": doc_id, "action": "list"})
    labels = [s.get("label") for s in listed.data.get("snapshots", [])]
    c.check("checkpoint" in labels, f"label listed: {labels}")
    back = await c.call(
        "history", {"doc_id": doc_id, "action": "restore", "snapshot_id": checkpoint}
    )
    c.check(bool(back.data.get("pre_restore_snapshot_id")), "restore snapshots first")
    c.check(await c.fingerprint(doc_id) == original, "restored to the checkpoint")

    src.write_text(FIXTURE_SVG.replace("Hello", "Reloaded"), encoding="utf-8")
    reloaded = await c.call("history", {"doc_id": doc_id, "action": "reload"})
    c.check(bool(reloaded.data.get("pre_reload_snapshot_id")), "reload snapshots first")
    texts = await c.objects(doc_id, {"text": "reloaded"})
    c.check("label" in ids_of(texts), "working copy re-read from the source file")
    pruned = await c.call("history", {"doc_id": doc_id, "action": "prune"})
    c.check("retained" in pruned.data, f"prune reports counts: {pruned.data}")
    msg = await c.fail("history", {"doc_id": doc_id, "action": "restore"})
    c.check("call history(action='list')" in msg, msg)


async def k11_export_variants(c: Ctx) -> None:
    """dry_run + byte_budget, save_as to a new file, then engine exports (profiles, sets)."""
    doc_id = await c.open_fixture("k11")
    other = await c.open_fixture("k11b")
    dry = await c.call(
        "export",
        {
            "doc_id": doc_id,
            "targets": [{"kind": "file", "format": "png", "width_px": 512}],
            "dry_run": True,
            "byte_budget": 10,
        },
    )
    c.check(dry.data.get("dry_run") is True and dry.data.get("projected_bytes", 0) > 0, "projected")
    c.check(dry.data.get("within_budget") is False, "a 10-byte budget is exceeded")
    c.check("artifacts" not in dry.data, "dry_run reports a plan, no artifacts")
    dest = c.ws / "k11-saved.svg"
    saved = await c.call(
        "export", {"doc_id": doc_id, "targets": [{"kind": "save_as", "dest_path": str(dest)}]}
    )
    c.check(dest.exists() and "<svg" in dest.read_text(encoding="utf-8"), "save_as wrote the file")
    c.check(not Path(str(saved.data["saved"]["path"])).is_absolute(), "saved path is relative")
    msg = await c.fail(
        "export",
        {
            "doc_id": [doc_id, other],
            "targets": [{"kind": "save_as", "dest_path": str(c.ws / "x.svg")}],
        },
    )
    c.check("call export once per doc_id" in msg, msg)

    c.needs_engine("file/profile export")
    icons = await c.call(
        "export", {"doc_id": doc_id, "targets": [{"kind": "profile", "name": "icons"}]}
    )
    c.check(len(icons.data.get("artifacts", [])) >= 2, "icon ladder written")
    pdf = await c.call(
        "export", {"doc_id": doc_id, "targets": [{"kind": "profile", "name": "print"}]}
    )
    c.check(any(a.get("format") == "pdf" for a in pdf.data.get("artifacts", [])), "print PDF")
    both = await c.call(
        "export", {"doc_id": [doc_id, other], "targets": [{"kind": "file", "format": "png"}]}
    )
    names = [Path(str(a.get("path"))).name for a in both.data.get("artifacts", [])]
    c.check(len(names) == 2, f"one PNG per document: {names}")
    c.check(
        any(f"-{doc_id}-" in n for n in names) and any(f"-{other}-" in n for n in names),
        f"file names carry the doc_id: {names}",
    )


async def k12_discovery(c: Ctx) -> None:
    """help(goal|op|topic) and the out-of-scope answer."""
    goal = await c.call("help", {"goal": "make the svg smaller for the web"})
    matches = goal.data["goal"].get("matches") or []
    uses = [u for m in matches if isinstance(m, dict) for u in m.get("use", [])]
    c.check("edit:optimize_web" in uses, f"goal maps to edit:optimize_web: {uses}")
    c.check(any(isinstance(m, dict) and m.get("example") for m in matches), "example call")
    oos = await c.call("help", {"goal": "retouch the pixels of this photo"})
    c.check(oos.data["goal"].get("out_of_scope") is True, "photo retouch is out of scope")
    op = await c.call("help", {"op": "fill"})
    c.check(op.data["op"].get("example", {}).get("op") == "fill", "op reference with example")
    ops = await c.call("help", {"topic": "ops"})
    c.check("groups" in ops.data.get("ops", {}), "ops index grouped")
    diag = await c.call("help", {"topic": "diagnose"})
    c.check("capabilities" in diag.data, "diagnose returns the capability matrix")
    msg = await c.fail("help", {"op": "bogus"})
    c.check("help(topic='ops')" in msg, msg)


async def k13_actionable_errors(c: Ctx) -> None:
    """Errors are one line, name the next call and never leak host paths."""
    doc_id = await c.open_fixture("k13")
    msg = await c.fail("inspect", {"doc_id": "d_gone"})
    # One server serves every scenario, so the list may be truncated before this doc_id.
    c.check(
        msg.startswith("document id not found; open ids: ")
        and (doc_id in msg or msg.endswith(" more)")),
        msg,
    )
    msg = await c.fail("open", {"path": "../../etc/passwd"})
    c.check(msg.startswith("path rejected:") and str(c.ws) not in msg and "/etc" not in msg, msg)
    msg = await c.fail("open", {"path": str(REPO_ROOT / "README.md")})
    c.check(msg.startswith("path rejected:") and str(REPO_ROOT) not in msg, msg)
    msg = await c.fail("export", {"doc_id": doc_id, "targets": [{"kind": "nope"}]})
    c.check(msg.startswith("invalid export arguments: ") and one_line(msg), msg)
    msg = await c.fail("inspect", {"doc_id": doc_id, "include": ["bogus"]})
    c.check(msg.startswith("invalid inspect arguments: ") and one_line(msg), msg)
    msg = await c.fail("edit", {"doc_id": doc_id, "ops": [{"op": "fill", "ids": ["bg"]}]})
    c.check("ops[0].fill.color: Field required" in msg, msg)
    msg = await c.fail(
        "open",
        {
            "canvas": {"width": 10, "height": 10},
            "ops": [{"op": "fill", "ids": ["x"], "color": "#000"}],
        },
    )
    c.check(msg.startswith("opened ") and "but ops failed" in msg, f"names the opened doc: {msg}")


# --- R: risk and approval --------------------------------------------------------------------


async def r1_delete_needs_token(c: Ctx) -> None:
    """delete without token is refused; dry_run plans it; with token it runs at risk high."""
    doc_id = await c.open_fixture("r1")
    before, snaps = await c.fingerprint(doc_id), await c.snapshot_count(doc_id)
    delete = [{"op": "delete", "ids": ["bg"]}]
    msg = await c.fail("edit", {"doc_id": doc_id, "ops": delete})
    c.check(msg == "ops[0] delete is HIGH risk; re-call with approval_token", msg)
    c.check(await c.fingerprint(doc_id) == before, "refused call changed nothing")
    c.check(await c.snapshot_count(doc_id) == snaps, "refused call took no snapshot")
    plan = await c.call("edit", {"doc_id": doc_id, "ops": delete, "dry_run": True})
    c.check(plan.data.get("needs_approval") is True and plan.data.get("risk") == "high", "plan")
    c.check(await c.fingerprint(doc_id) == before, "dry_run changed nothing")
    done = await c.call("edit", {"doc_id": doc_id, "ops": delete, "approval_token": TOKEN})
    c.check(done.data.get("risk") == "high" and done.data.get("changed") is True, "deleted")
    c.check(
        f"snapshot_id='{done.data.get('snapshot_id')}'" in str(done.data.get("undo")),
        "HIGH edit names its undo call",
    )
    c.check("bg" not in ids_of(await c.objects(doc_id)), "bg is gone")
    msg = await c.fail("edit", {"doc_id": doc_id, "ops": delete, "approval_token": TOKEN})
    c.check("not found" in msg, f"deleting an unknown id is an error: {msg}")


async def r2_max_risk(c: Ctx) -> None:
    """One delete among ten creates makes the whole call HIGH; nothing is created without token."""
    doc_id = await c.open_fixture("r2")
    count = len(await c.objects(doc_id))
    ops: list[dict[str, Any]] = [
        {"op": "create_rect", "x": i * 10, "y": 0, "width": 8, "height": 8, "fill": "#00aa00"}
        for i in range(10)
    ]
    ops.append({"op": "delete", "ids": ["label"]})
    msg = await c.fail("edit", {"doc_id": doc_id, "ops": ops})
    c.check(msg.startswith("ops[10] delete is HIGH risk"), msg)
    c.check(len(await c.objects(doc_id)) == count, "no rect created")
    plan = await c.call("edit", {"doc_id": doc_id, "ops": ops, "dry_run": True})
    c.check(plan.data.get("risk") == "high", "call risk is the maximum over the ops")
    low = await c.call("edit", {"doc_id": doc_id, "ops": ops[:10]})
    c.check(low.data.get("risk") == "medium" and len(low.data.get("created", [])) == 10, "medium")


async def r3_svg_ops_need_token(c: Ctx) -> None:
    """set_svg and insert_svg are HIGH."""
    doc_id = await c.open_fixture("r3")
    insert = [
        {
            "op": "insert_svg",
            "svg": '<circle xmlns="http://www.w3.org/2000/svg" id="ins" cx="5" cy="5" r="4"/>',
        }
    ]
    replace = [
        {
            "op": "set_svg",
            "svg": '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
            '<rect id="only" width="10" height="10"/></svg>',
        }
    ]
    for ops in (insert, replace):
        msg = await c.fail("edit", {"doc_id": doc_id, "ops": ops})
        c.check(msg.endswith("is HIGH risk; re-call with approval_token"), msg)
    await c.call("edit", {"doc_id": doc_id, "ops": insert, "approval_token": TOKEN})
    c.check("ins" in ids_of(await c.objects(doc_id)), "fragment inserted")
    await c.call("edit", {"doc_id": doc_id, "ops": replace, "approval_token": TOKEN})
    c.check(ids_of(await c.objects(doc_id)) == {"only"}, "document replaced")

    # Even an approved fragment goes through the scrubber: a <script> is refused or stripped.
    script = '<g xmlns="http://www.w3.org/2000/svg" id="evil"><script>alert(1)</script></g>'
    _, err = await c.attempt(
        "edit",
        {
            "doc_id": doc_id,
            "ops": [{"op": "insert_svg", "svg": script}],
            "approval_token": TOKEN,
        },
    )
    dest = c.ws / "r3-check.svg"
    await c.call(
        "export", {"doc_id": doc_id, "targets": [{"kind": "save_as", "dest_path": str(dest)}]}
    )
    c.check("<script" not in dest.read_text(encoding="utf-8"), f"no script in the document {err}")


async def r4_save_as_overwrite(c: Ctx) -> None:
    """save_as to an existing file needs overwrite=true AND a token."""
    doc_id = await c.open_fixture("r4")
    dest = c.ws / "r4-out.svg"
    target: dict[str, Any] = {"kind": "save_as", "dest_path": str(dest)}
    first = await c.call("export", {"doc_id": doc_id, "targets": [target]})
    c.check(dest.exists() and not first.data["saved"].get("overwritten"), "first save is new")
    await c.call(
        "edit", {"doc_id": doc_id, "ops": [{"op": "fill", "ids": ["bg"], "color": "#00ff00"}]}
    )
    old = sha256(dest)
    msg = await c.fail("export", {"doc_id": doc_id, "targets": [target]})
    c.check("overwrite=True and an approval_token" in msg, msg)
    msg = await c.fail("export", {"doc_id": doc_id, "targets": [{**target, "overwrite": True}]})
    c.check("approval_token" in msg, msg)
    c.check(sha256(dest) == old, "refused saves left the file alone")
    done = await c.call(
        "export",
        {"doc_id": doc_id, "targets": [{**target, "overwrite": True}], "approval_token": TOKEN},
    )
    c.check(done.data["saved"].get("overwritten") is True, "overwritten flagged")
    c.check(sha256(dest) != old, "file content replaced")


async def r5_blank_token(c: Ctx) -> None:
    """A whitespace-only token is not an approval."""
    doc_id = await c.open_fixture("r5")
    msg = await c.fail(
        "edit", {"doc_id": doc_id, "ops": [{"op": "delete", "ids": ["bg"]}], "approval_token": "  "}
    )
    c.check(msg == "ops[0] delete is HIGH risk; re-call with approval_token", msg)
    c.check("bg" in ids_of(await c.objects(doc_id)), "bg still there")


# --- runner ----------------------------------------------------------------------------------

Scenario = Callable[[Ctx], Awaitable[None]]

SCENARIOS: dict[str, Scenario] = {
    "K1": k1_logo_from_scratch,
    "K2": k2_recolor_and_web_export,
    "K3": k3_dollar_refs,
    "K4": k4_selector_instead_of_lookup,
    "K5": k5_atomic_rollback,
    "K6": k6_dry_run_and_detail,
    "K7": k7_noop,
    "K8": k8_inspect,
    "K9": k9_render,
    "K10": k10_history,
    "K11": k11_export_variants,
    "K12": k12_discovery,
    "K13": k13_actionable_errors,
    "R1": r1_delete_needs_token,
    "R2": r2_max_risk,
    "R3": r3_svg_ops_need_token,
    "R4": r4_save_as_overwrite,
    "R5": r5_blank_token,
}


def title(fn: Scenario) -> str:
    return (fn.__doc__ or "").strip().splitlines()[0]


def server_env(ws: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["INKSCAPE_MCP_WORKSPACE_ROOTS"] = str(ws)
    # Core surface only: live and raw actions stay off (scenarios cover neither).
    env["INKSCAPE_MCP_LIVE_ENABLED"] = "0"
    env["INKSCAPE_MCP_RAW_ACTION_ENABLED"] = "0"
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
            ctx = Ctx(client, ws, engine, args.require_inkscape)
            started = time.monotonic()
            status, note = "PASS", ""
            try:
                await SCENARIOS[key](ctx)
            except Skipped as exc:
                status, note = "SKIP", str(exc)
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
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    parser.add_argument("--only", help="comma-separated scenario ids, e.g. K1,R1")
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
        parser.error(f"unknown scenario(s): {', '.join(unknown)}; see --list")

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
