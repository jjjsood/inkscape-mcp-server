"""Read-only SVG validation engine (E1-08, ADR-005 direct DOM).

Pure functions over the WORKING COPY of a registered document. Builds on the E1-04
inspection engine (`inkscape_mcp.document.inspect`) for fonts / assets / viewBox data and
on the foundation subprocess wrapper (`inkscape_mcp.workspace.subprocess_exec`) to query
installed fonts via `fc-list`. No MCP decorators here; the tool layer
(`inkscape_mcp.tools.validate`) wraps `validate_document` and maps errors to `ToolError`.

READ-ONLY by contract (sec.12, task E1-08): nothing here mutates the working copy or the
original. No repair / auto-fix in E1 (deferred to E2).

Findings carry a STABLE machine `code`, a `severity` (`error` | `warning` | `info`), a
human-readable `message` that NEVER contains a host path, and an optional `locator` (the id,
font family, or href the finding is about). `ValidationReport.ok` is True iff there are no
`error`-severity findings.

Id-check interpretation (documented for downstream reuse):

- **duplicate_id** (severity `error`): the same `id` value appears on two or more elements.
  SVG ids must be document-unique, so any repeat is a hard error. One finding per duplicated
  id value (not one per offending element).
- **missing_id** (severity `error`): a reference (`#frag`, `url(#frag)`, or
  `xlink:href="#frag"` / `href="#frag"`) points at an id that no element in the document
  defines. We do NOT flag anonymous elements that simply lack an `id` — an element without an
  id is legal SVG; only a *dangling reference* (a pointer with no target) is the problem this
  check means.
"""

from __future__ import annotations

import base64
import binascii
import re
from urllib.parse import unquote

from lxml import etree
from pydantic import BaseModel

from inkscape_mcp.document.inspect import (
    GENERIC_FONT_KEYWORDS,
    DocumentNotFound,
    InspectionError,
    _build_parent_map,
    _CssRule,
    _document_css_rules,
    _font_families_from_value,
    _load_tree,
    _own_paint_value,
    inspect_assets,
    inspect_fonts,
    inspect_summary,
    installed_font_families,
)
from inkscape_mcp.fonts.coverage import suggest_covering_family, uncovered_chars
from inkscape_mcp.logging_setup import get_logger
from inkscape_mcp.registry import Registry, get_registry

__all__ = [
    "LARGE_RASTER_BYTES",
    "DocumentNotFound",
    "Finding",
    "InspectionError",
    "ValidationReport",
    "validate_document",
]

_logger = get_logger("validate")

# Embedded-raster size threshold: a `data:` image whose decoded byte length exceeds this is
# flagged `large_raster` (a warning, not an error). 5 MiB by default.
LARGE_RASTER_BYTES = 5 * 1024 * 1024

# Namespace-qualified href attribute name (xlink), mirroring the inspection engine.
XLINK_NS = "http://www.w3.org/1999/xlink"
_XLINK_HREF = f"{{{XLINK_NS}}}href"

# Matches `url(#id)` references inside attribute / style values.
_URL_REF_RE = re.compile(r"url\(\s*['\"]?#([^'\")\s]+)['\"]?\s*\)")
# Attributes that may carry a `url(#id)` / `#id` reference to another element.
_REF_ATTRS = (
    "fill",
    "stroke",
    "mask",
    "clip-path",
    "filter",
    "style",
    "marker-start",
    "marker-mid",
    "marker-end",
)


class Finding(BaseModel):
    """One validation problem.

    `code` is a stable machine identifier (e.g. `missing_font`, `duplicate_id`); `severity` is
    one of `error` | `warning` | `info`; `message` is human-readable and host-path-free;
    `locator` names what the finding is about (an element id, font family, or href), or None
    when not applicable.
    """

    code: str
    severity: str
    message: str
    locator: str | None = None


class ValidationReport(BaseModel):
    """Structured, machine-readable validation result for one document.

    `ok` is True iff there are no `error`-severity findings. `error_count` / `warning_count`
    are convenience tallies over `findings`.
    """

    doc_id: str
    ok: bool
    findings: list[Finding]
    error_count: int
    warning_count: int


# --- fc-list (installed fonts) ----------------------------------------------


def _installed_font_families() -> tuple[set[str] | None, str | None]:
    """Return (families, note).

    Thin wrapper over the SINGLE-SOURCE detection in the inspection engine
    (`inkscape_mcp.document.inspect.installed_font_families`) so V3 (`missing_font`) here and the
    `inspect_document` / fonts-resource `available` flag never diverge. On success `families` is the
    set of lowercased installed family names and `note` is None; when the font database can't be
    queried, `families` is None and `note` is a short reason for the `info` finding. Never raises.
    """
    families = installed_font_families()
    if families is None:
        return None, "fontconfig unavailable"
    return families, None


def _check_missing_fonts(doc_id: str, findings: list[Finding]) -> None:
    """Flag referenced font families that are not installed (warning).

    Degrades gracefully: if `fc-list` is unavailable the check is skipped with a single `info`
    finding rather than a crash or a false warning. Uses the shared installed-font detection and the
    shared generic-keyword set so this stays in lockstep with the `available` flag elsewhere.
    """
    referenced = inspect_fonts(doc_id).fonts
    if not referenced:
        return

    installed, note = _installed_font_families()
    if installed is None:
        findings.append(
            Finding(
                code="font_check_skipped",
                severity="info",
                message=f"font availability not checked: {note}",
                locator=None,
            )
        )
        return

    for font in referenced:
        family = font.family.strip()
        low = family.lower()
        if not family or low in GENERIC_FONT_KEYWORDS:
            continue
        if low not in installed:
            findings.append(
                Finding(
                    code="missing_font",
                    severity="warning",
                    message=f"referenced font family is not installed: {family!r}",
                    locator=family,
                )
            )


def _check_external_assets(doc_id: str, findings: list[Finding]) -> None:
    """Flag each external asset reference (warning).

    `data:` URIs and in-document `#fragment` refs are NOT external (the inspection engine
    already excludes them from `AssetInfo.external`).
    """
    for asset in inspect_assets(doc_id).assets:
        if asset.external:
            findings.append(
                Finding(
                    code="external_asset",
                    severity="warning",
                    message=(
                        f"external {asset.kind} reference may break if the file moves: {asset.href}"
                    ),
                    locator=asset.href,
                )
            )


def _data_uri_decoded_size(href: str) -> int | None:
    """Estimate the decoded byte length of a `data:` URI payload.

    Returns the byte length for a base64 `data:` URI, the raw (percent-decoded) length for a
    non-base64 `data:` URI, or None if `href` is not a `data:` URI. Never fetches anything.
    """
    h = href.strip()
    if not h.lower().startswith("data:"):
        return None
    comma = h.find(",")
    if comma == -1:
        return 0
    header = h[len("data:") : comma].lower()
    payload = h[comma + 1 :]
    if "base64" in header:
        # Estimate decoded size; tolerate stray whitespace and missing padding.
        compact = re.sub(r"\s+", "", payload)
        try:
            return len(base64.b64decode(compact, validate=False))
        except (ValueError, binascii.Error):
            # Fall back to the standard 3-bytes-per-4-chars estimate.
            return (len(compact) * 3) // 4
    # Non-base64 data URI: payload is percent-encoded text.
    return len(unquote(payload).encode("utf-8", errors="replace"))


def _href_of(elem: etree._Element) -> str | None:
    return elem.get(_XLINK_HREF) or elem.get("href")


def _check_large_rasters(root: etree._Element, findings: list[Finding]) -> None:
    """Flag embedded `data:` raster images whose decoded size exceeds the threshold (warning).

    External `<image>` refs are handled by the external-asset check, not here. No network
    access: only the in-document `data:` payload is measured.
    """
    for elem in root.iter():
        if not isinstance(elem.tag, str):
            continue
        if etree.QName(elem.tag).localname != "image":
            continue
        href = _href_of(elem)
        if not href:
            continue
        size = _data_uri_decoded_size(href)
        if size is None:  # external image — covered by external-asset check
            continue
        if size > LARGE_RASTER_BYTES:
            elem_id = elem.get("id")
            locator = elem_id if elem_id else href[:32]
            mib = size / (1024 * 1024)
            findings.append(
                Finding(
                    code="large_raster",
                    severity="warning",
                    message=(
                        f"embedded raster image is large ({mib:.1f} MiB > "
                        f"{LARGE_RASTER_BYTES // (1024 * 1024)} MiB); consider linking or "
                        "downscaling"
                    ),
                    locator=locator,
                )
            )


def _referenced_ids(elem: etree._Element) -> set[str]:
    """Collect the local ids this element references via `#id` / `url(#id)`."""
    refs: set[str] = set()
    href = _href_of(elem)
    if href and href.startswith("#"):
        frag = href[1:].strip()
        if frag:
            refs.add(frag)
    for attr in _REF_ATTRS:
        val = elem.get(attr)
        if not val:
            continue
        for match in _URL_REF_RE.finditer(val):
            frag = match.group(1).strip()
            if frag:
                refs.add(frag)
    return refs


def _check_ids(root: etree._Element, findings: list[Finding]) -> None:
    """Flag duplicate ids (error) and dangling `#id` references (error).

    See the module docstring for the precise interpretation of duplicate vs. missing id.
    """
    seen: dict[str, int] = {}
    defined: set[str] = set()
    referenced: dict[str, None] = {}  # ordered set of referenced fragment ids

    for elem in root.iter():
        if not isinstance(elem.tag, str):
            continue
        elem_id = elem.get("id")
        if elem_id:
            seen[elem_id] = seen.get(elem_id, 0) + 1
            defined.add(elem_id)
        for ref in _referenced_ids(elem):
            referenced.setdefault(ref, None)

    for dup_id, count in seen.items():
        if count > 1:
            findings.append(
                Finding(
                    code="duplicate_id",
                    severity="error",
                    message=f"id is used by {count} elements; ids must be document-unique",
                    locator=dup_id,
                )
            )

    for ref in referenced:
        if ref not in defined:
            findings.append(
                Finding(
                    code="missing_id",
                    severity="error",
                    message=f"reference points at id {ref!r} but no element defines it",
                    locator=ref,
                )
            )


def _check_doctype(root: etree._Element, findings: list[Finding]) -> None:
    """Surface a DOCTYPE / DTD so a hostile entity is OBSERVABLE, not just silently neutralized.

    The safe parser (`resolve_entities=False`, `no_network=True`, `load_dtd=False`) never expands
    entities or fetches anything, so an external entity such as
    ``<!ENTITY xxe SYSTEM "file:///etc/hostname">`` is inert, but `validate_document` previously
    reported `ok:true, findings:[]`, giving an agent zero signal a malicious DOCTYPE was present
    (E13-02). This emits an EXTERNAL-ENTITY warning per declared external entity (SYSTEM/PUBLIC, a
    classic XXE vector) and a `doctype_present` info finding for any DOCTYPE at all (rare in SVG).
    It changes NOTHING and never reduces `ok` below an `error`: the parse is already safe; the point
    is visibility.
    """
    dtd = root.getroottree().docinfo.internalDTD
    if dtd is None:
        return

    saw_external = False
    # `iterentities` exists on the lxml DTD object at runtime but is absent from the type stubs;
    # fetch it defensively so a stub gap (or a malformed DTD) never raises.
    iter_entities = getattr(dtd, "iterentities", None)
    try:
        entities = list(iter_entities()) if iter_entities is not None else []
    except Exception:  # pragma: no cover - defensive: malformed DTD object
        entities = []
    for entity in entities:
        # External entities carry a system/public identifier and no inline content; an internal
        # entity (`<!ENTITY x "literal">`) carries `content` and no `system_url`.
        system_url = getattr(entity, "system_url", None)
        if system_url:
            saw_external = True
            name = getattr(entity, "name", None) or "?"
            findings.append(
                Finding(
                    code="external_entity",
                    severity="warning",
                    message=(
                        f"document declares external entity {name!r}; it is NOT expanded "
                        "(safe parse, no network), but external entities are an XXE vector"
                    ),
                    locator=name,
                )
            )

    if not saw_external:
        findings.append(
            Finding(
                code="doctype_present",
                severity="info",
                message=(
                    "document declares a DOCTYPE/DTD; SVG normally has none and it is ignored on a "
                    "safe parse"
                ),
                locator=None,
            )
        )


# --- glyph coverage (E16-04) ------------------------------------------------

# Text-bearing SVG element local names. A glyph-coverage check only makes sense for elements that
# actually carry rendered text content.
_TEXT_TAGS = frozenset({"text", "tspan", "textPath", "tref", "flowRoot", "flowPara", "flowSpan"})


def _qname_local(tag: object) -> str:
    """Local element name (namespace stripped); '' for comments / PIs."""
    if not isinstance(tag, str):
        return ""
    return etree.QName(tag).localname


def _effective_font_family(
    elem: etree._Element,
    css_rules: list[_CssRule],
    parents: dict[etree._Element, etree._Element],
) -> str | None:
    """Resolve the effective FIRST `font-family` for a text element, folding cascade + inheritance.

    `font-family` is an inherited CSS property, so an element with no own value takes the nearest
    ancestor's. At each level the value is resolved with the SAME cascade `find_objects` uses
    (`<style>` rules by specificity/order < presentation attribute < inline `style`). The reported
    family is the FIRST name in the comma list (the one the renderer tries first and the one the
    saved SVG names) — generic CSS keywords (`sans-serif`, ...) never reach coverage analysis.
    """
    current: etree._Element | None = elem
    while current is not None:
        own = _own_paint_value(current, css_rules, "font-family")
        if own is not None and own.strip().lower() != "inherit":
            families = _font_families_from_value(own)
            return families[0] if families else None
        current = parents.get(current)
    return None


def _check_glyph_coverage(root: etree._Element, findings: list[Finding]) -> None:
    """Flag text whose declared font family cannot actually render its characters (warning).

    Per text element: resolve the effective FIRST `font-family`, read THAT family's own cmap (never
    fontconfig substitution — see :mod:`inkscape_mcp.fonts.coverage`), and report any character the
    family does not cover plus a cmap-verified covering family suggestion. The render may "look
    right" via fontconfig auto-substitution while the SAVED SVG still names the non-covering family,
    so this is the correctness signal an agent needs (E16-04). Degrades to silence when coverage is
    unknown (family not installed — owned by `missing_font` — or fontconfig unavailable) and never
    fires for fully-covered text, whitespace, or generic keywords.
    """
    css_rules = _document_css_rules(root)
    parents = _build_parent_map(root)

    # Only flag each (family, element) once: walk the OUTERMOST text element and use its full text
    # content, so nested <tspan>s don't double-report. A <tspan> with its OWN family override is
    # still reached as a distinct element below.
    seen: set[int] = set()
    for elem in root.iter():
        if not isinstance(elem.tag, str):
            continue
        if _qname_local(elem.tag) not in _TEXT_TAGS:
            continue
        # Skip a text node nested inside another text node we already covered with the SAME family —
        # but DO analyse a child that declares its own family (a real override).
        parent = parents.get(elem)
        if (
            parent is not None
            and id(parent) in seen
            and _own_paint_value(elem, css_rules, "font-family") is None
        ):
            continue

        family = _effective_font_family(elem, css_rules, parents)
        if not family:
            continue
        text = "".join(t for t in elem.itertext() if isinstance(t, str))
        if not text.strip():
            continue

        missing = uncovered_chars(family, text)
        if missing is None or missing == "":
            # None = coverage unknown (not installed / no fontconfig); "" = fully covered. Either
            # way this is not a missing-glyph defect.
            continue

        seen.add(id(elem))
        suggestion = suggest_covering_family(missing)
        elem_id = elem.get("id")
        suffix = f"; try {suggestion!r}" if suggestion else ""
        findings.append(
            Finding(
                code="missing_glyphs",
                severity="warning",
                message=(
                    f"font {family!r} cannot render {missing!r} in this text; the saved SVG "
                    f"names a family that would show tofu on a renderer without font "
                    f"substitution{suffix}"
                ),
                locator=elem_id if elem_id else family,
            )
        )


def _check_viewbox(doc_id: str, findings: list[Finding]) -> None:
    """Flag a missing viewBox (warning) or a malformed / non-positive one (error).

    `inspect_summary` parses `viewBox` to four floats; it returns None both when the attribute
    is absent AND when it is present but unparseable. To tell those apart (missing → warning vs.
    invalid → error) we re-read the raw attribute from the root element.
    """
    summary = inspect_summary(doc_id)
    if summary.viewbox is not None:
        _, _, width, height = summary.viewbox
        if width <= 0 or height <= 0:
            findings.append(
                Finding(
                    code="viewbox_invalid",
                    severity="error",
                    message="viewBox width/height must be positive",
                    locator=f"{summary.viewbox}",
                )
            )
        return

    # viewbox is None: distinguish absent (warning) from present-but-malformed (error).
    _entry, root = _load_tree(doc_id)
    raw = root.get("viewBox")
    if raw is None:
        findings.append(
            Finding(
                code="viewbox_missing",
                severity="warning",
                message="document has no viewBox; coordinate scaling is undefined",
                locator=None,
            )
        )
    else:
        findings.append(
            Finding(
                code="viewbox_invalid",
                severity="error",
                message="viewBox is present but not four valid numbers",
                locator=raw,
            )
        )


def validate_document(doc_id: str, registry: Registry | None = None) -> ValidationReport:
    """Run all read-only checks over a document and return a structured report.

    Checks: missing fonts, glyph coverage (E16-04 — per text element, a `missing_glyphs` warning
    when the declared family's OWN cmap cannot render the text, computed independently of fontconfig
    auto-substitution, with a cmap-verified covering-family suggestion), external assets, large
    embedded rasters, id problems (duplicate ids and dangling `#id` references), viewBox presence /
    sanity, and DOCTYPE/external-entity presence (E13-02 — the entity is never expanded by the safe
    parser, but a hostile DOCTYPE is surfaced as a finding so it is observable rather than silently
    neutralized). Resolves `doc_id`
    via the registry; raises `DocumentNotFound` for an unknown id and `InspectionError` if the
    working copy cannot be parsed safely (the tool layer maps both to `ToolError`).

    Validate vs. quality split (E10-06 V2): `validate_document` reports CORRECTNESS problems —
    things that are wrong or risky (broken references, duplicate ids, an invalid viewBox, an
    uninstalled font, an external/oversized asset). It deliberately does NOT flag "cruft" /
    optimization opportunities (editor-only metadata, unused `<defs>`, unreferenced ids, empty
    groups, reducible coordinate precision): those are not defects, they are clean-up suggestions
    and live in `quality_report` as `opportunities` (sourced from the E5-04 optimizer, exactly what
    `svg_web_optimize` would strip). Keeping cruft out of validation means `ValidationReport.ok`
    stays a true correctness gate rather than a style opinion. Use `quality_report` for the cruft
    signal.

    READ-ONLY: never mutates the working copy or the original.
    """
    reg = registry if registry is not None else get_registry()
    # Resolve up-front so an unknown id raises before any check runs.
    try:
        reg.get(doc_id)
    except KeyError:
        raise DocumentNotFound("document id not found") from None

    findings: list[Finding] = []

    # Parse the working copy once for the element-level checks (ids, rasters).
    _entry, root = _load_tree(doc_id)

    _check_missing_fonts(doc_id, findings)
    _check_glyph_coverage(root, findings)
    _check_external_assets(doc_id, findings)
    _check_large_rasters(root, findings)
    _check_ids(root, findings)
    _check_viewbox(doc_id, findings)
    _check_doctype(root, findings)

    error_count = sum(1 for f in findings if f.severity == "error")
    warning_count = sum(1 for f in findings if f.severity == "warning")
    return ValidationReport(
        doc_id=doc_id,
        ok=error_count == 0,
        findings=findings,
        error_count=error_count,
        warning_count=warning_count,
    )
