"""Adopt agent-composed SVG into a tracked working copy (ADR-004/005, sec.12).

Two ``mutate(tree) -> str`` builders plus the SVG-string safe-parse + STRICT allowlist scrubber:

- :func:`make_set_document_svg` replaces the WHOLE working-copy document with an agent-composed SVG
  string (the root must be ``<svg>``).
- :func:`make_insert_svg_fragment` inserts an agent-composed fragment under a parent (by id) or the
  document root.

Both go through the shared, reversible edit pipeline (`apply_edit`) at ``RiskClass.HIGH`` —
wholesale or sub-tree replacement from untrusted, free-form SVG is the highest-risk path, so it is
approval-gated and snapshotted (ADR-004). All editing is direct lxml (ADR-005); no Inkscape engine.

HARDENING (sec.12): the incoming SVG STRING is parsed through the NORMATIVE safe parser
(:func:`inkscape_mcp.workspace.xml_safety.parse_svg_string` — XXE off, no network, no entity
expansion, huge_tree off), then every element and attribute is walked and checked against a STRICT
allowlist. Anything not on the allowlist is REJECTED outright (fail-closed), with a stable,
host-path-free message. Specifically rejected:

- ``<script>`` (and any non-allowlisted element),
- any ``on*`` event-handler attribute (``onload``, ``onclick``, …),
- any non-allowlisted attribute,
- a ``javascript:`` (or other active-content) scheme in any href/url-bearing attribute,
- an EXTERNAL href — ``http(s)://``, scheme-relative ``//``, ``file:``, and ``data:`` — in any
  href-bearing attribute. Only a same-document ``#id`` fragment reference is permitted.

The allowlist is the safe authoring vocabulary (shapes, structure, paint defs, text); it
deliberately omits ``<image>``/``<foreignObject>``/``<a>``/``<style>``/``<script>`` and any
external/active reference so a composed document can carry no network, no script, and no embedded
raster (default reject ``data:``; if embedded images are ever needed that is a separate epic).
"""

from __future__ import annotations

from lxml import etree

from inkscape_mcp.document.inspect import INKSCAPE_NS, SODIPODI_NS, SVG_NS, XLINK_NS
from inkscape_mcp.edit.dom import (
    EditError,
    TargetNotFound,
    fmt_num,
    normalize_color,
    parse_viewbox,
    require_target,
)
from inkscape_mcp.edit.pipeline import MutateFn
from inkscape_mcp.workspace.xml_safety import UnsafeXMLError, parse_svg_string

#: Allowed SVG element local-names (the safe authoring vocabulary). Deliberately EXCLUDES `script`,
#: `image`, `foreignObject`, `a`, `style`, `animate*`, `set`, and anything that can carry active
#: content or an external reference. A non-allowlisted element is rejected (fail-closed).
ALLOWED_ELEMENTS = frozenset(
    {
        "svg",
        "g",
        "defs",
        "symbol",
        "use",
        "title",
        "desc",
        "metadata",
        # shapes
        "rect",
        "circle",
        "ellipse",
        "line",
        "polygon",
        "polyline",
        "path",
        # text
        "text",
        "tspan",
        "textPath",
        # paint servers / clip / mask / marker
        "linearGradient",
        "radialGradient",
        "stop",
        "pattern",
        "clipPath",
        "mask",
        "marker",
    }
)

#: Allowed attribute local-names that may appear on ANY allowed element. Presentation/geometry/text
#: attributes plus structural ids; NO `on*` event handler is here, and href is handled specially
#: (same-document `#id` only). A non-allowlisted attribute is rejected.
ALLOWED_ATTRS = frozenset(
    {
        # identity / structure
        "id",
        "class",
        "transform",
        "viewBox",
        "preserveAspectRatio",
        "version",
        "xmlns",
        "width",
        "height",
        "x",
        "y",
        "x1",
        "y1",
        "x2",
        "y2",
        "cx",
        "cy",
        "r",
        "rx",
        "ry",
        "d",
        "points",
        "dx",
        "dy",
        "rotate",
        "offset",
        "gradientUnits",
        "gradientTransform",
        "spreadMethod",
        "fx",
        "fy",
        "patternUnits",
        "patternContentUnits",
        "patternTransform",
        "clipPathUnits",
        "maskUnits",
        "maskContentUnits",
        "markerUnits",
        "markerWidth",
        "markerHeight",
        "refX",
        "refY",
        "orient",
        # presentation
        "style",
        "fill",
        "fill-opacity",
        "fill-rule",
        "stroke",
        "stroke-width",
        "stroke-opacity",
        "stroke-linecap",
        "stroke-linejoin",
        "stroke-dasharray",
        "stroke-dashoffset",
        "stroke-miterlimit",
        "opacity",
        "color",
        "stop-color",
        "stop-opacity",
        "display",
        "visibility",
        "clip-path",
        "clip-rule",
        "mask",
        "marker-start",
        "marker-mid",
        "marker-end",
        "filter",
        # text
        "font-family",
        "font-size",
        "font-style",
        "font-weight",
        "text-anchor",
        "letter-spacing",
        "word-spacing",
        "text-decoration",
        "dominant-baseline",
        "alignment-baseline",
        "white-space",
        "xml:space",
    }
)

#: Namespaces whose elements/attributes are accepted (their LOCAL name must still be allowlisted).
#: SVG (default + explicit), xlink (href only, validated), and the inkscape/sodipodi editor
#: namespaces (label/groupmode/etc. — inert metadata, never active content).
_ALLOWED_NAMESPACES = frozenset({None, SVG_NS, XLINK_NS, INKSCAPE_NS, SODIPODI_NS})

#: Attribute local-names that carry a URI/reference and must pass the href guard (same-document
#: `#id` only — no external scheme, no `javascript:`).
_HREF_LIKE = frozenset({"href"})

#: Attribute local-names whose VALUE may contain a `url(#id)` paint reference (also href-guarded for
#: the embedded `url(...)` target). A bare `#id` or `url(#id)` is allowed; an external/`javascript:`
#: target inside `url(...)` is rejected.
_URL_REF_ATTRS = frozenset(
    {
        "fill",
        "stroke",
        "clip-path",
        "mask",
        "filter",
        "marker-start",
        "marker-mid",
        "marker-end",
        "style",
    }
)


class ComposeError(EditError):
    """Adopted SVG violated the allowlist / href policy or could not be parsed safely.

    A subclass of :class:`EditError` so the pipeline's `mutate`-error path handles it uniformly; the
    tool layer maps it to a stable, host-path-free `ToolError`.
    """


def _local_name(name: str) -> str:
    """Local (un-namespaced) name of a tag/attribute Clark-notation string."""
    if name.startswith("{"):
        return name.split("}", 1)[1]
    return name


def _namespace(name: str) -> str | None:
    """Namespace URI of a Clark-notation tag/attribute name, or None for the un-namespaced form."""
    if name.startswith("{"):
        return name.split("}", 1)[0][1:]
    return None


def _reject_external_or_active(value: str, *, attr: str) -> None:
    """Reject an external scheme or `javascript:`/active-content target in a reference value.

    Permits ONLY an empty value or a same-document `#fragment`. Any scheme-bearing or
    scheme-relative value (`http(s)://`, `//`, `file:`, `data:`, `javascript:`, `mailto:`, …) is
    rejected (default-deny on anything that is not a bare `#id`). Used for plain href values.
    """
    v = value.strip()
    if v == "" or v.startswith("#"):
        return
    raise ComposeError(
        f"svg contains disallowed external/active reference in {attr!r}: only same-document "
        "'#id' references are allowed"
    )


def _check_url_refs(value: str, *, attr: str) -> None:
    """For a paint/url-bearing attribute or `style`, reject any non-same-document `url(...)` target.

    A value may legitimately carry `url(#grad)`; a `url(http://…)` / `url(data:…)` /
    `url(javascript:…)` is rejected. Also catches a `javascript:` scheme appearing bare in a
    `style` value (e.g. a CSS active-content trick). The check is conservative: any `url(` whose
    target does not begin with `#` is rejected.
    """
    low = value.lower()
    if "javascript:" in low:
        raise ComposeError(f"svg contains disallowed 'javascript:' reference in {attr!r}")
    idx = 0
    while True:
        idx = low.find("url(", idx)
        if idx == -1:
            return
        inner = value[idx + 4 :].lstrip().lstrip("'\"").lstrip()
        if not inner.startswith("#"):
            raise ComposeError(
                f"svg contains disallowed external reference in a url(...) in {attr!r}: only "
                "same-document 'url(#id)' references are allowed"
            )
        idx += 4


def _scrub_element(elem: etree._Element) -> None:
    """Validate one element + its attributes against the allowlist, or raise `ComposeError`.

    Element rule: a non-element node (comment / PI) is rejected outright; the element's namespace
    must be allowed AND its local name must be in :data:`ALLOWED_ELEMENTS`. Attribute rule: each
    attribute's namespace must be allowed, its local name must be in :data:`ALLOWED_ATTRS` (or be a
    validated `href`), no `on*` event handler is permitted, and any href/url-bearing value must pass
    the external/active reference guard. Fail-closed: anything unrecognized is rejected.
    """
    tag = elem.tag
    if not isinstance(tag, str):
        # Comment / ProcessingInstruction / entity — never allowed in adopted content.
        raise ComposeError("svg contains a disallowed node (comment/processing-instruction)")

    ns = _namespace(tag)
    local = _local_name(tag)
    if ns not in _ALLOWED_NAMESPACES:
        raise ComposeError(f"svg contains disallowed element namespace: {ns!r}")
    if local not in ALLOWED_ELEMENTS:
        raise ComposeError(f"svg contains disallowed element: {local!r}")

    for attr_name, raw_value in elem.attrib.items():
        if not isinstance(attr_name, str):  # pragma: no cover - lxml attrib keys are always str
            raise ComposeError("svg contains a disallowed attribute")
        # lxml types attribute values as `str | bytes`; SVG values are always text — decode a bytes
        # value defensively so the downstream str-only guards typecheck and never crash.
        attr_value = (
            raw_value if isinstance(raw_value, str) else raw_value.decode("utf-8", "replace")
        )
        a_ns = _namespace(attr_name)
        a_local = _local_name(attr_name)
        a_low = a_local.lower()

        # Block every event-handler attribute regardless of namespace (onload/onclick/...).
        if a_low.startswith("on"):
            raise ComposeError(f"svg contains a disallowed event-handler attribute: {a_local!r}")

        if a_ns not in _ALLOWED_NAMESPACES:
            raise ComposeError(f"svg contains disallowed attribute namespace: {a_ns!r}")

        # href (plain or xlink:href): same-document '#id' only.
        if a_local in _HREF_LIKE:
            _reject_external_or_active(attr_value, attr=a_local)
            continue

        # inkscape:/sodipodi: editor metadata — accept any local name in those namespaces (inert).
        if a_ns in (INKSCAPE_NS, SODIPODI_NS):
            continue

        if a_local not in ALLOWED_ATTRS:
            raise ComposeError(f"svg contains a disallowed attribute: {a_local!r}")

        # Paint / url-bearing / style values: no external or javascript: url(...) target.
        if a_local in _URL_REF_ATTRS:
            _check_url_refs(attr_value, attr=a_local)


def scrub_tree(root: etree._Element) -> None:
    """Walk `root` and every descendant through :func:`_scrub_element`, raising on the first miss.

    The single allowlist-enforcement entry point. Mutates nothing; it only validates. After this
    returns the subtree is known to contain ONLY allowlisted elements/attributes with no `on*`
    handler and no external/`javascript:` reference, so it is safe to graft into a working copy.
    """
    for elem in root.iter():
        _scrub_element(elem)


def parse_and_scrub(svg: str) -> etree._Element:
    """Safe-parse an SVG STRING and enforce the allowlist; return the validated root element.

    Two-stage hardening (sec.12): (1) the NORMATIVE safe parser neutralizes XXE / billion-laughs /
    external-DTD; (2) :func:`scrub_tree` enforces the element/attribute allowlist + href policy.
    Raises :class:`ComposeError` for an unparseable input (mapped from `UnsafeXMLError`) or for any
    allowlist/href violation — both stable and host-path-free.
    """
    try:
        tree = parse_svg_string(svg)
    except UnsafeXMLError as exc:
        raise ComposeError("document could not be parsed safely") from exc
    root = tree.getroot()
    scrub_tree(root)
    return root


def build_blank_svg(
    width: float,
    height: float,
    viewbox: str | None = None,
    background: str | None = None,
) -> bytes:
    """Build a blank, `validate_document`-clean SVG document as UTF-8 bytes.

    `width` / `height` are the page size in user units (both must be finite and > 0). `viewbox` is
    an optional explicit `viewBox` ("minx miny w h", four numbers); when omitted a `0 0 W H`
    box is synthesized so the document is never viewBox-less (validation flags a missing viewBox).
    `background` is an optional colour (validated via :func:`dom.normalize_color` — hex / rgb() /
    hsl() / named keyword only, never CSS-injectable) painted as a full-page `<rect>`; omitted is a
    transparent page.

    The result is round-tripped through :func:`parse_and_scrub` so a generated document can never
    carry anything outside the allowlist (defense in depth) and so the bytes are exactly what a
    later safe parse will accept. Raises :class:`ComposeError` for a non-positive/garbage size or a
    bad viewBox / background.
    """
    if not _positive_finite(width):
        raise ComposeError("width must be a finite number greater than 0")
    if not _positive_finite(height):
        raise ComposeError("height must be a finite number greater than 0")
    sw, sh = fmt_num(float(width)), fmt_num(float(height))

    if viewbox is not None:
        nums = parse_viewbox(viewbox)
        if nums is None:
            raise ComposeError("viewBox must be four numbers: 'minx miny width height'")
        if nums[2] <= 0 or nums[3] <= 0:
            raise ComposeError("viewBox width/height must be positive")
        vb = " ".join(fmt_num(n) for n in nums)
    else:
        vb = f"0 0 {sw} {sh}"

    # An optional full-page background rect (colour validated → no CSS/markup injection possible).
    bg = ""
    if background is not None:
        color = normalize_color(background)
        bg = f'<rect id="background" x="0" y="0" width="100%" height="100%" fill="{color}"/>'

    # Build from validated tokens only (sw/sh/vb come from fmt_num/parse_viewbox; color is
    # normalize_color-validated), then round-trip through the safe parser + allowlist scrubber so
    # the generated bytes are exactly what a later safe parse accepts (defense in depth).
    text = f'<svg xmlns="{SVG_NS}" width="{sw}" height="{sh}" viewBox="{vb}">{bg}</svg>'
    root = parse_and_scrub(text)
    return etree.tostring(etree.ElementTree(root), xml_declaration=True, encoding="UTF-8")


def _positive_finite(value: float) -> bool:
    """True iff `value` coerces to a finite float strictly greater than 0."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return False
    return num > 0 and num == num and num not in (float("inf"), float("-inf"))


def make_set_document_svg(svg: str) -> MutateFn:
    """Build a `mutate` closure that REPLACES the whole document with an agent-composed SVG string.

    The incoming `svg` is safe-parsed + allowlist-scrubbed up front (so a bad input fails before any
    pipeline side effect), and its root MUST be an `<svg>` element (a fragment is rejected — use
    :func:`make_insert_svg_fragment` for that). On apply, the working tree's root element is
    replaced wholesale with the validated root. Reversible via the pipeline's pre-mutation snapshot.
    """
    new_root = parse_and_scrub(svg)
    if _local_name(new_root.tag) != "svg":
        raise ComposeError("set_document_svg requires the SVG root element to be <svg>")

    def mutate(tree: etree._ElementTree) -> str:
        old_root = tree.getroot()
        # Replace the tree's root in place: graft the validated root over the parsed tree's root by
        # swapping the underlying element. lxml has no setroot on a parsed tree, so we re-point the
        # tree by replacing the old root with the new one at the document level.
        parent = old_root.getparent()
        if parent is not None:  # pragma: no cover - a document root has no parent
            parent.replace(old_root, new_root)
        else:
            tree._setroot(new_root)
        return "replaced document with composed svg"

    return mutate


def make_insert_svg_fragment(
    svg: str, parent_id: str | None = None, unwrap: bool = True
) -> MutateFn:
    """Build a `mutate` closure that inserts an agent-composed SVG fragment under a parent.

    The incoming `svg` is safe-parsed + allowlist-scrubbed up front (so a bad/disallowed fragment
    fails before any pipeline side effect). XML requires a single root element, so the fragment is
    ONE element subtree (wrap several siblings in a `<g>` to insert them as a unit). On apply that
    validated element is appended under the target parent — `parent_id` (must exist in the document)
    or the document root when omitted. Reversible via the pipeline's pre-mutation snapshot.

    `unwrap` (default `True`, the historical behaviour) controls how a `<svg>` ROOT is handled:

    - `unwrap=True` — a `<svg>` root used purely as a wrapper to hold several sibling shapes is
      UNWRAPPED: its child elements are grafted under the parent and the wrapper `<svg>` itself is
      dropped. An EMPTY wrapper has nothing to graft and is rejected.
    - `unwrap=False` — the `<svg>` root is KEPT and inserted AS-IS as a nested `<svg>` element (it
      is in :data:`ALLOWED_ELEMENTS`, so it still passes the scrubber). An empty nested `<svg>` is
      allowed in this mode, since the caller explicitly asked for the container verbatim.

    `unwrap` has no effect on a non-`<svg>` root: any other single element (a `<g>`, `<rect>`, …) is
    always inserted intact. Raises :class:`ComposeError` for a bad/disallowed fragment (or an empty
    `<svg>` wrapper when `unwrap=True`) and `TargetNotFound` for an absent `parent_id`.
    """
    frag_root = parse_and_scrub(svg)

    def mutate(tree: etree._ElementTree) -> str:
        root = tree.getroot()
        parent = require_target(root, parent_id) if parent_id is not None else root
        # With `unwrap` on, a wrapper <svg> is unwrapped (graft its children); with it off, the
        # <svg> is kept as an explicit nested container. Any non-<svg> single element is inserted
        # as-is regardless. This lets a caller pass `<svg>…multiple shapes…</svg>` and get the
        # shapes (default), opt to keep the nested `<svg>`, or pass one `<g>`/`<rect>`/… intact.
        if unwrap and _local_name(frag_root.tag) == "svg":
            children = [c for c in frag_root if isinstance(c.tag, str)]
            if not children:
                raise ComposeError("composed fragment <svg> wrapper is empty")
            for child in children:
                parent.append(child)
            return f"inserted {len(children)} element(s) from composed fragment"
        parent.append(frag_root)
        return f"inserted <{_local_name(frag_root.tag)}> from composed fragment"

    return mutate


__all__ = [
    "ALLOWED_ATTRS",
    "ALLOWED_ELEMENTS",
    "ComposeError",
    "EditError",
    "TargetNotFound",
    "build_blank_svg",
    "make_insert_svg_fragment",
    "make_set_document_svg",
    "parse_and_scrub",
    "scrub_tree",
]
