"""Reference data for the op union: one example per op plus the group each op belongs to.

The `edit` tool's schema deliberately does NOT carry the op union (it would be ~60 KB on every
turn); `help(op=...)` and `help(topic="ops")` serve this reference instead. Every example is
validated against the union in the test suite, so the docs cannot drift from the models.
"""

from typing import Any, get_args

from inkscape_mcp.edit.ops import Op

#: One valid example op per op name. Ops that act on objects show `ids`; `select` works anywhere
#: `ids` does (`{"select": {"tag": "rect", "fill": "#ff0000"}}`). `"$<n>"` refers to the id the
#: n-th op of the same call created.
OP_EXAMPLES: dict[str, dict[str, Any]] = {
    "create_rect": {
        "op": "create_rect",
        "x": 0,
        "y": 0,
        "width": 200,
        "height": 100,
        "fill": "#3366ff",
        "id": "bg",
    },
    "create_circle": {"op": "create_circle", "cx": 50, "cy": 50, "r": 40, "fill": "#ffcc00"},
    "create_ellipse": {"op": "create_ellipse", "cx": 50, "cy": 50, "rx": 40, "ry": 20},
    "create_line": {
        "op": "create_line",
        "x1": 0,
        "y1": 0,
        "x2": 100,
        "y2": 100,
        "stroke": "#000000",
        "stroke_width": "2",
    },
    "create_polygon": {
        "op": "create_polygon",
        "points": [[0, 0], [100, 0], [50, 80]],
        "fill": "#cc3333",
    },
    "create_polyline": {
        "op": "create_polyline",
        "points": [[0, 0], [50, 40], [100, 0]],
        "stroke": "#000000",
    },
    "create_path": {"op": "create_path", "d": "M10 10 L90 10 L50 80 Z", "fill": "#228833"},
    "create_text": {"op": "create_text", "x": 20, "y": 60, "text": "Hello", "fill": "#222222"},
    "create_group": {"op": "create_group", "id": "logo"},
    "create_use": {"op": "create_use", "href": "bg", "x": 10, "y": 10},
    "linear_gradient": {
        "op": "linear_gradient",
        "id": "sky",
        "stops": [{"offset": 0, "color": "#ffffff"}, {"offset": 1, "color": "#3366ff"}],
    },
    "radial_gradient": {
        "op": "radial_gradient",
        "id": "glow",
        "stops": [{"offset": 0, "color": "#ffff00"}, {"offset": 1, "color": "#ff8800"}],
    },
    "group": {"op": "group", "ids": ["a", "b"], "id": "pair"},
    "reparent": {"op": "reparent", "ids": ["a"], "parent": "pair"},
    "fill": {"op": "fill", "ids": ["bg"], "color": "#ff0000"},
    "stroke": {"op": "stroke", "ids": ["bg"], "color": "#000000", "width": "2"},
    "opacity": {"op": "opacity", "ids": ["bg"], "opacity": 0.5},
    "replace_color": {
        "op": "replace_color",
        "from_color": "#ff0000",
        "to_color": "#00aa00",
    },
    "palette": {"op": "palette", "mapping": {"#ff0000": "#00aa00", "#0000ff": "#222222"}},
    "font": {"op": "font", "select": {"tag": "text"}, "family": "DejaVu Sans", "size": "24"},
    "replace_text": {"op": "replace_text", "ids": ["title"], "text": "New title"},
    "duplicate": {"op": "duplicate", "ids": ["bg"], "new_id": "bg2"},
    "rename": {"op": "rename", "ids": ["bg"], "new_id": "background", "label": "Background"},
    "delete": {"op": "delete", "ids": ["bg"]},
    "move": {"op": "move", "ids": ["bg"], "dx": 10, "dy": -5},
    "scale": {"op": "scale", "ids": ["bg"], "sx": 1.5},
    "rotate": {"op": "rotate", "ids": ["bg"], "degrees": 45},
    "tile": {"op": "tile", "ids": ["bg"], "rows": 2, "cols": 3, "dx": 120, "dy": 120},
    "resize_canvas": {"op": "resize_canvas", "width": "400", "height": "300"},
    "normalize_viewbox": {"op": "normalize_viewbox"},
    "fit_to_content": {"op": "fit_to_content"},
    "set_svg": {
        "op": "set_svg",
        "svg": '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>',
    },
    "insert_svg": {
        "op": "insert_svg",
        "svg": '<circle xmlns="http://www.w3.org/2000/svg" cx="5" cy="5" r="4"/>',
    },
    "place": {"op": "place", "source_doc_id": "doc-other", "x": 10, "y": 10, "scale": 0.5},
    "grid": {"op": "grid", "doc_ids": ["doc-a", "doc-b"], "rows": 1, "cols": 2, "cell": 128},
    "simplify": {"op": "simplify", "ids": ["p1"]},
    "union": {"op": "union", "ids": ["p1", "p2"]},
    "difference": {"op": "difference", "ids": ["p1", "p2"]},
    "combine": {"op": "combine", "ids": ["p1", "p2"]},
    "break_apart": {"op": "break_apart", "ids": ["p1"]},
    "stroke_to_path": {"op": "stroke_to_path", "ids": ["p1"]},
    "cleanup": {"op": "cleanup", "ids": ["p1"]},
    "optimize_web": {"op": "optimize_web", "precision": 3},
}

#: Group -> op names, in presentation order (matches the table in docs/rework-plan.md).
OP_GROUPS: dict[str, list[str]] = {
    "create": [
        "create_rect",
        "create_circle",
        "create_ellipse",
        "create_line",
        "create_polygon",
        "create_polyline",
        "create_path",
        "create_text",
        "create_group",
        "create_use",
        "linear_gradient",
        "radial_gradient",
        "group",
        "reparent",
    ],
    "style": ["fill", "stroke", "opacity", "replace_color", "palette", "font"],
    "object": ["replace_text", "duplicate", "rename", "delete"],
    "transform": [
        "move",
        "scale",
        "rotate",
        "tile",
        "resize_canvas",
        "normalize_viewbox",
        "fit_to_content",
    ],
    "compose": ["set_svg", "insert_svg", "place", "grid"],
    "path": [
        "simplify",
        "union",
        "difference",
        "combine",
        "break_apart",
        "stroke_to_path",
        "cleanup",
    ],
    "optimize": ["optimize_web"],
}


def op_models() -> dict[str, Any]:
    """`op name -> pydantic model class`, read off the discriminated union."""
    (union,) = (a for a in get_args(Op) if get_args(a))
    models: dict[str, Any] = {}
    for cls in get_args(union):
        (name,) = get_args(cls.model_fields["op"].annotation)
        models[name] = cls
    return models
