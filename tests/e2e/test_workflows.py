"""Realistic multi-step agent workflows, end to end against real Inkscape.

Every test is one client session that chains 15-30 calls the way an agent works through a task:
look, change, look again, recover from mistakes, ship files. Each step's input comes from the
previous step's output, and the shipped artifacts are verified by content and pixels.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.e2e.conftest import SVG_NS, Session, by_id, near, pixel, text_of

pytestmark = pytest.mark.inkscape

TEAL, NAVY, OLD_RED, OLD_NAVY = "#2a9d8f", "#264653", "#e63946", "#1d3557"


def ids(res: Any) -> list[str]:
    """Object ids from an `inspect(select=...)` result (rows are `[id, tag, bbox, fill]`)."""
    return sorted(row[0] for row in res["objects"].get("objects", []))


def artifacts(res: Any, fmt: str) -> list[dict[str, Any]]:
    return [a for a in res["artifacts"] if a["format"] == fmt]


def test_brand_refresh_of_a_layered_inkscape_poster(run_flow: Any) -> None:
    """Rebrand a real Inkscape poster (layers, CSS classes, gradient, symbol, tspans), ship it."""

    async def flow(s: Session) -> None:
        src = s.fixture("brand_poster.svg", "poster.svg")
        original = s.file(src).read_bytes()

        opened = await s("open", path=src, render="inline")
        doc = opened["doc_id"]
        assert opened["summary"]["layers"] == 3
        assert len(opened.images) == 1
        assert near(pixel(opened.image(), 20, 290, 400), OLD_RED)

        # Orient: layers, palette in use, health.
        info = await s("inspect", doc_id=doc, include=["layers", "styles", "validate"])
        assert [la["label"] for la in info["layers"]["layers"]] == [
            "Background",
            "Content",
            "Footer",
        ]
        assert {OLD_RED, OLD_NAVY} <= set(info["styles"]["colors"])
        assert info["validate"]["ok"] is True

        # Which objects carry the old accent? Includes the CSS-class-painted ones and the symbol.
        accent = await s("inspect", doc_id=doc, select={"fill": OLD_RED})
        assert ids(accent) == ["band", "burst", "cta", "logo-ring"]
        headline = await s("inspect", doc_id=doc, select={"text": "spring"})
        assert "headline" in ids(headline)

        checkpoint = await s("history", doc_id=doc, action="create", label="before-rebrand")

        # The whole rebrand in ONE call.
        rebrand = await s(
            "edit",
            doc_id=doc,
            ops=[
                {"op": "palette", "mapping": {OLD_RED: TEAL, OLD_NAVY: NAVY}},
                {"op": "replace_text", "ids": ["headline"], "text": "Summer Sale"},
                {"op": "replace_text", "ids": ["cta-label"], "text": "Shop the drop"},
            ],
            render="inline",
            detail="summary",
        )
        assert rebrand["changed"] is True
        img = rebrand.image()
        assert near(pixel(img, 20, 290, 400), TEAL), "CSS .accent band must be recoloured"
        assert near(pixel(img, 330, 90, 400), TEAL), "CSS .accent burst must be recoloured"
        assert near(pixel(img, 2, 2, 400), NAVY), "gradient stop must be recoloured"

        # Verify through the model, not just the picture.
        assert ids(await s("inspect", doc_id=doc, select={"fill": OLD_RED})) == []
        assert ids(await s("inspect", doc_id=doc, select={"fill": TEAL})) == [
            "band",
            "burst",
            "cta",
            "logo-ring",
        ]
        styles = await s("inspect", doc_id=doc, include=["styles"])
        assert OLD_RED not in styles["styles"]["colors"]
        assert TEAL in styles["styles"]["colors"]

        cta = await s("render", doc_id=doc, target="object", object_id="cta", width_px=280)
        assert cta["width_px"] == 280
        assert near(cta.image().getpixel((20, 20)), TEAL)

        # Keep an editable master, then optimise a web copy.
        await s("export", doc_id=doc, targets=[{"kind": "save_as", "dest_path": "out/master.svg"}])
        tidy = await s("edit", doc_id=doc, ops=[{"op": "optimize_web"}], detail="summary")
        assert tidy["changed"] is True
        # Still editable after optimizing: ids and layers survive the default pass.
        assert ids(await s("inspect", doc_id=doc, select={"id_prefix": "headline"})) == [
            "headline",
            "headline-line",
        ]
        layers = await s("inspect", doc_id=doc, include=["layers"])
        assert len(layers["layers"]["layers"]) == 3

        # Final ship-only pass drops ids/layers: HIGH risk, refused without approval.
        msg = await s.fails("edit", doc_id=doc, ops=[{"op": "optimize_web", "aggressive": True}])
        assert "approval_token" in msg
        ship = await s(
            "edit",
            doc_id=doc,
            ops=[{"op": "optimize_web", "aggressive": True}],
            approval_token="ship-it",
        )
        assert ship["changed"] is True and ship["snapshot_id"] in ship["undo"]
        shipped = await s(
            "export",
            doc_id=doc,
            targets=[
                {"kind": "profile", "name": "web"},
                {"kind": "file", "format": "pdf"},
                {"kind": "save_as", "dest_path": "out/web.svg"},
            ],
        )
        assert shipped["saved"]["path"] == "out/web.svg"

        # Master: layers, CSS, texts all intact and rebranded.
        master = s.svg("out/master.svg")
        labels = master.xpath(
            "//svg:g[@inkscape:groupmode='layer']/@inkscape:label",
            namespaces={"svg": SVG_NS, "inkscape": "http://www.inkscape.org/namespaces/inkscape"},
        )
        assert labels == ["Background", "Content", "Footer"]
        css = by_id(master, "style1").text or ""
        assert TEAL in css and OLD_RED not in css
        assert text_of(master, "headline") == "Summer Sale"
        assert by_id(master, "headline-line").text == "Summer Sale"  # Inkscape line tspan kept
        assert text_of(master, "cta-label") == "Shop the drop"
        assert text_of(master, "footer-text") == "acme.example · 2026"

        # Web copy: editor cruft gone, content and colours kept.
        web_svg = s.file("out/web.svg").read_text(encoding="utf-8")
        assert "sodipodi:namedview" not in web_svg
        assert "Summer Sale" in web_svg and TEAL in web_svg

        (png,) = artifacts(shipped, "png")
        assert (png["width_px"], png["height_px"]) == (1024, 768)
        web_png = s.png(png["path"])
        assert near(pixel(web_png, 20, 290, 400), TEAL)
        assert near(pixel(web_png, 330, 90, 400), TEAL)
        (pdf,) = [a for a in shipped["artifacts"] if a["kind"] == "file"]
        assert s.file(pdf["path"]).read_bytes().startswith(b"%PDF")

        # The source file is never touched; history holds both edits and the checkpoint.
        assert s.file(src).read_bytes() == original
        hist = await s("history", doc_id=doc)
        assert checkpoint["snapshot_id"] in [h["id"] for h in hist["snapshots"]]
        assert hist["count"] >= 3

    run_flow(flow)


def test_icon_set_built_from_a_blank_canvas(run_flow: Any) -> None:
    """Draw an app icon with `$n` refs + gradient in one call, iterate, ship an icon profile."""

    async def flow(s: Session) -> None:
        opened = await s(
            "open",
            canvas={"width": 64, "height": 64, "viewBox": "0 0 64 64"},
            ops=[
                {
                    "op": "linear_gradient",
                    "id": "bg-grad",
                    "stops": [{"offset": 0, "color": "#ff8800"}, {"offset": 1, "color": "#ff3300"}],
                },
                {
                    "op": "create_rect",
                    "x": 0,
                    "y": 0,
                    "width": 64,
                    "height": 64,
                    "rx": 14,
                    "fill": "url(#bg-grad)",
                    "id": "tile",
                },
                {"op": "create_circle", "cx": 32, "cy": 32, "r": 18, "fill": "#ffffff"},
                {"op": "create_circle", "cx": 32, "cy": 32, "r": 8, "fill": "#222222"},
                {"op": "group", "ids": ["$2", "$3"], "id": "eye"},
            ],
            render="inline",
        )
        doc = opened["doc_id"]
        created = {c["op"]: c["id"] for c in opened["created"]}
        assert created[4] == "eye"
        img = opened.image()
        assert near(pixel(img, 32, 32, 64), "#222222")
        assert near(pixel(img, 32, 18, 64), "#ffffff")
        assert near(pixel(img, 4, 32, 64), "#ff8800", tol=60)

        # Look closer at the eye, then tweak it: smaller pupil, nudge up, add a highlight.
        eye = await s("render", doc_id=doc, target="object", object_id="eye", width_px=128)
        assert eye["width_px"] == 128
        pupil = created[3]
        tweak = await s(
            "edit",
            doc_id=doc,
            ops=[
                {"op": "scale", "ids": [pupil], "sx": 0.75},
                {"op": "move", "ids": ["eye"], "dx": 0, "dy": -4},
                {
                    "op": "create_circle",
                    "cx": 36,
                    "cy": 24,
                    "r": 3,
                    "fill": "#ffffff",
                    "id": "glint",
                },
                {"op": "reparent", "ids": ["$2"], "parent": "eye"},
            ],
            render="inline",
        )
        assert tweak["changed"] is True
        assert near(pixel(tweak.image(), 32, 28, 64), "#222222")

        tree = await s("inspect", doc_id=doc, include=["tree", "validate", "quality"])
        assert tree["validate"]["ok"] is True

        # Dry-run the export plan, then ship it.
        plan = await s(
            "export",
            doc_id=doc,
            targets=[{"kind": "profile", "name": "icons", "sizes": [16, 32, 64, 128]}],
            dry_run=True,
        )
        assert plan.get("artifacts", []) == []
        shipped = await s(
            "export",
            doc_id=doc,
            targets=[
                {"kind": "profile", "name": "icons", "sizes": [16, 32, 64, 128]},
                {"kind": "save_as", "dest_path": "icons/app.svg"},
            ],
            name_prefix="app",
        )
        pngs = artifacts(shipped, "png")
        assert sorted(a["width_px"] for a in pngs) == [16, 32, 64, 128]
        for a in pngs:
            im = s.png(a["path"])
            assert im.size == (a["width_px"], a["width_px"])
            assert im.getpixel((0, 0))[3] < 40, "rounded corner must stay transparent"
            if im.width >= 32:  # the pupil (now centred at 32,28) is a blur below that
                assert near(pixel(im, 32, 28, 64), "#222222", tol=50)

        svg = s.svg("icons/app.svg")
        assert by_id(svg, "glint").getparent().get("id") == "eye"
        assert "url(#bg-grad)" in (
            by_id(svg, "tile").get("style") or by_id(svg, "tile").get("fill")
        )

    run_flow(flow)


def test_iterative_design_with_mistakes_and_recovery(run_flow: Any) -> None:
    """Make wrong edits, refuse a risky one, restore, approve, reload after an outside change."""

    async def flow(s: Session) -> None:
        src = s.fixture("brand_poster.svg", "poster.svg")
        doc = (await s("open", path=src))["doc_id"]
        good = await s("history", doc_id=doc, action="create", label="good")

        # Mistake 1: moved the wrong thing far off the page.
        # The logo ring (symbol via <use> in the translated header) covers page (32,44).
        oops = await s(
            "edit", doc_id=doc, ops=[{"op": "move", "ids": ["header"], "dx": 900}], render="inline"
        )
        assert oops["changed"] is True
        assert not near(pixel(oops.image(), 32, 44, 400), OLD_RED)
        restored = await s("history", doc_id=doc, action="restore", snapshot_id=good["snapshot_id"])
        assert restored["restored_from"] == good["snapshot_id"]
        page = await s("render", doc_id=doc)
        assert near(pixel(page.image(), 32, 44, 400), OLD_RED)

        # Mistake 2: delete without approval is refused and changes nothing.
        before = (await s("history", doc_id=doc))["count"]
        msg = await s.fails("edit", doc_id=doc, ops=[{"op": "delete", "ids": ["burst"]}])
        assert "approval" in msg
        assert (await s("history", doc_id=doc))["count"] == before
        plan = await s(
            "edit", doc_id=doc, ops=[{"op": "delete", "select": {"tag": "path"}}], dry_run=True
        )
        assert plan["dry_run"] is True and plan["needs_approval"] is True
        matched = plan["plan"][0]["matched"]
        assert "burst" in matched

        # Approve a scoped delete; the result tells how to undo it.
        gone = await s(
            "edit",
            doc_id=doc,
            ops=[{"op": "delete", "ids": ["burst"]}],
            approval_token="ok-delete-burst",
            render="inline",
        )
        assert gone["changed"] is True
        assert near(pixel(gone.image(), 330, 90, 400), "#e63946") is False
        assert gone["snapshot_id"] in gone["undo"]
        assert ids(await s("inspect", doc_id=doc, select={"id_prefix": "burst"})) == []

        # Change of heart: undo exactly via the hint's snapshot.
        await s("history", doc_id=doc, action="restore", snapshot_id=gone["snapshot_id"])
        assert ids(await s("inspect", doc_id=doc, select={"id_prefix": "burst"})) == ["burst"]

        # A no-op edit is reported as such and records nothing.
        count = (await s("history", doc_id=doc))["count"]
        noop = await s(
            "edit",
            doc_id=doc,
            ops=[{"op": "replace_color", "from_color": "#123456", "to_color": "#000000"}],
        )
        assert noop["changed"] is False
        assert (await s("history", doc_id=doc))["count"] == count

        # Someone edits the source file outside the server; reload picks it up, reversibly.
        s.file(src).write_text(
            s.file(src).read_text(encoding="utf-8").replace("Spring Sale", "Autumn Sale"),
            encoding="utf-8",
        )
        reloaded = await s("history", doc_id=doc, action="reload")
        assert reloaded["summary"]["layers"] == 3
        assert "headline" in ids(await s("inspect", doc_id=doc, select={"text": "autumn"}))
        await s(
            "history", doc_id=doc, action="restore", snapshot_id=reloaded["pre_reload_snapshot_id"]
        )
        assert ids(await s("inspect", doc_id=doc, select={"text": "autumn"})) == []

        pruned = await s("history", doc_id=doc, action="prune")
        assert pruned["retained"] >= 1
        final = await s("inspect", doc_id=doc, include=["validate"])
        assert final["validate"]["ok"] is True

    run_flow(flow)


def test_multi_document_contact_sheet(run_flow: Any) -> None:
    """Open several sources, compose a sheet with grid + place, export it as PDF and PNG."""

    colors = ["#e63946", "#2a9d8f", "#e9c46a", "#264653"]

    async def flow(s: Session) -> None:
        tiles = []
        for i, color in enumerate(colors):
            svg = (
                f'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" '
                f'viewBox="0 0 100 100"><rect id="sw{i}" x="0" y="0" width="100" height="100" '
                f'fill="{color}"/><circle id="dot{i}" cx="50" cy="50" r="20" fill="#ffffff"/></svg>'
            )
            tiles.append((await s("open", svg=svg))["doc_id"])
        poster = (await s("open", path=s.fixture("brand_poster.svg", "poster.svg")))["doc_id"]

        sheet = await s(
            "open",
            canvas={"width": 440, "height": 440, "viewBox": "0 0 440 440", "background": "#ffffff"},
        )
        sid = sheet["doc_id"]
        composed = await s(
            "edit",
            doc_id=sid,
            ops=[
                {"op": "grid", "doc_ids": tiles, "rows": 2, "cols": 2, "cell": 100, "gap": 20},
                {"op": "place", "source_doc_id": poster, "x": 240, "y": 300, "scale": 0.4},
            ],
            render="inline",
            detail="summary",
        )
        assert composed["changed"] is True
        img = composed.image()
        # Grid cells sit at (0,0) (120,0) (0,120) (120,120); sample each swatch's corner.
        for (cx, cy), color in zip([(5, 5), (125, 5), (5, 125), (125, 125)], colors, strict=True):
            assert near(pixel(img, cx, cy, 440), color), color
        assert near(pixel(img, 50, 50, 440), "#ffffff")

        # Retitle the whole sheet: recolour one swatch everywhere it occurs.
        await s(
            "edit",
            doc_id=sid,
            ops=[{"op": "replace_color", "from_color": "#e9c46a", "to_color": "#f4a261"}],
        )
        view = await s("render", doc_id=sid, width_px=880)
        assert view["width_px"] == 880
        assert near(pixel(view.image(), 5, 125, 440), "#f4a261")

        # Source tiles are untouched by the composition.
        tile_view = await s("render", doc_id=tiles[2])
        assert near(pixel(tile_view.image(), 5, 5, 100), "#e9c46a")

        shipped = await s(
            "export",
            doc_id=sid,
            targets=[
                {"kind": "file", "format": "pdf"},
                {"kind": "file", "format": "png", "width_px": 440},
                {"kind": "save_as", "dest_path": "sheets/contact.svg"},
            ],
            name_prefix="sheet",
        )
        (png,) = artifacts(shipped, "png")
        sheet_png = s.png(png["path"])
        assert sheet_png.size == (440, 440)
        assert near(sheet_png.getpixel((125, 5)), "#2a9d8f")
        (pdf,) = artifacts(shipped, "pdf")
        assert s.file(pdf["path"]).read_bytes().startswith(b"%PDF")
        assert "sheet" in pdf["path"]

        # Batch export across all open tiles in one call.
        batch = await s("export", doc_id=tiles, targets=[{"kind": "file", "format": "png"}])
        assert len(artifacts(batch, "png")) == len(tiles)
        assert "Acme Studio" in s.file("sheets/contact.svg").read_text(encoding="utf-8")

    run_flow(flow)


def _crowd_svg(n_cols: int = 12, n_rows: int = 10) -> str:
    cells = []
    for r in range(n_rows):
        for c in range(n_cols):
            fill = "#cc0000" if (r + c) % 3 == 0 else "#0055cc"
            cells.append(
                f'<rect id="c{r}_{c}" x="{c * 40 + 5}" y="{r * 40 + 5}" width="30" height="30" '
                f'fill="{fill}"/>'
            )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{n_cols * 40}" height="{n_rows * 40}" '
        f'viewBox="0 0 {n_cols * 40} {n_rows * 40}">{"".join(cells)}</svg>'
    )


def test_bulk_selector_edits_on_a_large_document(run_flow: Any) -> None:
    """Find, recolour, move and delete subsets of 120 objects purely by selectors."""

    async def flow(s: Session) -> None:
        doc = (await s("open", svg=_crowd_svg()))["doc_id"]
        summary = await s("inspect", doc_id=doc, include=["summary"])
        assert summary["summary"]["num_objects"] >= 120

        reds = await s("inspect", doc_id=doc, select={"fill": "#cc0000"}, limit=500)
        n_red = reds["objects"]["count"]
        assert n_red == sum(1 for r in range(10) for c in range(12) if (r + c) % 3 == 0)

        # Top-left quadrant reds only (bbox intersection [x, y, w, h]).
        quad = {"fill": "#cc0000", "bbox": [0, 0, 240, 200]}
        in_quad = ids(await s("inspect", doc_id=doc, select=quad, limit=500))
        assert in_quad and all(int(i[1:].split("_")[1]) < 6 for i in in_quad)

        recolor = await s(
            "edit",
            doc_id=doc,
            ops=[
                {"op": "fill", "select": quad, "color": "#00aa44"},
                {"op": "move", "select": {"fill": "#00aa44"}, "dx": 0, "dy": 2},
            ],
            detail="full",
        )
        assert recolor["outcomes"][0]["matched"] and len(recolor["outcomes"][0]["matched"]) == len(
            in_quad
        )
        assert sorted(recolor["outcomes"][1]["matched"]) == sorted(in_quad)
        greens = await s("inspect", doc_id=doc, select={"fill": "#00aa44"}, limit=500)
        assert greens["objects"]["count"] == len(in_quad)
        reds_left = (await s("inspect", doc_id=doc, select={"fill": "#cc0000"}, limit=500))[
            "objects"
        ]["count"]
        assert reds_left == n_red - len(in_quad)

        # A too-broad selector is rejected (max_matches) instead of silently editing everything.
        msg = await s.fails(
            "edit",
            doc_id=doc,
            ops=[{"op": "fill", "select": {"tag": "rect", "max_matches": 10}, "color": "#000000"}],
        )
        assert "match" in msg

        # Delete the remaining reds with approval after previewing the count.
        plan = await s(
            "edit",
            doc_id=doc,
            ops=[{"op": "delete", "select": {"fill": "#cc0000", "max_matches": 200}}],
            dry_run=True,
        )
        assert len(plan["plan"][0]["matched"]) == reds_left
        await s(
            "edit",
            doc_id=doc,
            ops=[{"op": "delete", "select": {"fill": "#cc0000", "max_matches": 200}}],
            approval_token="bulk-delete",
        )
        after = await s("inspect", doc_id=doc, include=["summary"])
        assert after["summary"]["num_objects"] == summary["summary"]["num_objects"] - reds_left

        page = await s("render", doc_id=doc, width_px=480)
        img = page.image()
        assert near(pixel(img, 20, 22, 480), "#00aa44")  # c0_0 was red, in quad -> green, moved
        assert near(pixel(img, 60, 20, 480), "#0055cc")  # c0_1 blue untouched
        assert pixel(img, 420, 340, 480)[3] < 40  # c8_10 red outside quad -> deleted

        budget = await s(
            "export",
            doc_id=doc,
            targets=[{"kind": "file", "format": "png", "width_px": 4000}],
            byte_budget=1000,
            dry_run=True,
        )
        assert budget["within_budget"] is False
        assert budget["projected_bytes"] > 1000
        msg = await s.fails(
            "export",
            doc_id=doc,
            targets=[{"kind": "file", "format": "png", "width_px": 4000}],
            byte_budget=1000,
        )
        assert "byte budget" in msg

    run_flow(flow)


def test_atomic_failure_mid_flow_then_corrected_retry(run_flow: Any) -> None:
    """A long call with one bad op changes nothing; the agent fixes it and retries."""

    async def flow(s: Session) -> None:
        src = s.fixture("brand_poster.svg", "poster.svg")
        doc = (await s("open", path=src))["doc_id"]
        before_hist = (await s("history", doc_id=doc))["count"]
        before = await s("render", doc_id=doc, width_px=200)

        long_call = [
            {"op": "create_rect", "x": 300, "y": 180, "width": 80, "height": 36, "fill": "#ffffff"},
            {
                "op": "create_text",
                "x": 340,
                "y": 204,
                "text": "-30%",
                "fill": "#e63946",
                "anchor": "middle",
            },
            {"op": "group", "ids": ["$0", "$1"], "id": "badge"},
            {"op": "rotate", "ids": ["$2"], "degrees": -8},
            {"op": "replace_text", "ids": ["headline"], "text": "Final Sale"},
            {"op": "fill", "ids": ["no-such-object"], "color": "#000000"},
            {"op": "move", "ids": ["header"], "dx": 0, "dy": 4},
        ]
        msg = await s.fails("edit", doc_id=doc, ops=long_call)
        assert "ops[5]" in msg

        # Nothing at all changed: no badge, old headline, same history, same pixels.
        assert ids(await s("inspect", doc_id=doc, select={"id_prefix": "badge"})) == []
        assert "headline" in ids(await s("inspect", doc_id=doc, select={"text": "spring"}))
        assert (await s("history", doc_id=doc))["count"] == before_hist
        same = await s("render", doc_id=doc, width_px=200)
        assert list(same.image().getdata()) == list(before.image().getdata())

        # Find the right id, fix the op, retry the whole call.
        cta = await s("inspect", doc_id=doc, select={"text": "shop"})
        fixed = [dict(op) for op in long_call]
        fixed[5] = {"op": "fill", "ids": [ids(cta)[0]], "color": "#ffffff"}
        dry = await s("edit", doc_id=doc, ops=fixed, dry_run=True)
        assert [p["op"] for p in dry["plan"]] == list(range(len(fixed)))
        done = await s("edit", doc_id=doc, ops=fixed, render="inline", detail="summary")
        assert done["changed"] is True
        assert len(done["ops"]) == len(fixed)
        assert (await s("history", doc_id=doc))["count"] == before_hist + 1
        assert ids(await s("inspect", doc_id=doc, select={"id_prefix": "badge"})) == ["badge"]
        assert near(pixel(done.image(), 330, 200, 400), "#ffffff", tol=40)

        saved = await s(
            "export", doc_id=doc, targets=[{"kind": "save_as", "dest_path": "final.svg"}]
        )
        assert saved["saved"]["path"] == "final.svg"
        root = s.svg("final.svg")
        assert text_of(root, "headline") == "Final Sale"
        badge = by_id(root, "badge")
        # Rotated about its own centre, inside the current (Content) layer, not the locked one.
        assert badge.get("transform") == "rotate(-8,340,198)"
        assert badge.getparent().get("id") == "layer-content"
        assert "-30%" in "".join(badge.itertext())

        # Saving again to the same path needs explicit overwrite + approval.
        msg = await s.fails(
            "export", doc_id=doc, targets=[{"kind": "save_as", "dest_path": "final.svg"}]
        )
        assert "already exists" in msg and "approval_token" in msg
        again = await s(
            "export",
            doc_id=doc,
            targets=[{"kind": "save_as", "dest_path": "final.svg", "overwrite": True}],
            approval_token="overwrite-final",
        )
        assert again["saved"]["path"] == "final.svg"

    run_flow(flow)
