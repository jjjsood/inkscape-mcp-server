"""Export-profile engine.

Pure functions (no MCP decorators) that compose the existing CLI render/export engine
(`inkscape_mcp.render.cli.export_document`) into three reproducible export profiles:

- **web** — web-oriented defaults: a responsive PNG set (by explicit `widths` or `scales`, else a
  single width) plus one plain SVG.
- **icon** — a multi-size square PNG icon set produced from a single source asset.
- **print** — a print-oriented PDF (vector) with real print-specific export settings (a pinned PDF
  version that always changes the output, plus text-to-path), reported in `applied_settings`.

A "profile" here is a deterministic recipe: the same input document plus the same profile always
requests the same set of formats/sizes in the same order. (The underlying export filenames carry
a UTC timestamp by design, so successive runs do not clobber; reproducibility means the recipe is
fixed, not that filenames are identical.)

Everything delegates to `export_document`, which already enforces the §4 export limits (pixel cap
before invocation, output-size cap after), the per-process timeout, and arg-lists-only invocation
(`shell=False`) per sec.12. This module adds only the per-profile size validation that must happen
BEFORE Inkscape is invoked (icon sizes), and returns workspace-relative artifact paths verbatim.

This module raises the engine exception types directly (`RenderError`, `LimitExceeded`, `KeyError`
for an unknown document id, plus `ProcessError`); the tool layer maps them to stable `ToolError`
messages.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from inkscape_mcp.config import Settings, get_settings
from inkscape_mcp.logging_setup import get_logger
from inkscape_mcp.render.cli import RenderResult, export_document

_logger = get_logger("render.profiles")

#: Profile identifiers (stable tokens carried on the result models).
WEB = "web"
ICON = "icon"
PRINT = "print"

#: Default raster width (px) for the web profile's PNG. A common 1x hero/content width.
DEFAULT_WEB_WIDTH_PX = 1024

#: Default icon set sizes (px, square). A conventional favicon/app-icon ladder.
DEFAULT_ICON_SIZES: tuple[int, ...] = (16, 32, 48, 64, 128, 256)

#: Default density multipliers for the responsive web set. 1x/2x/3x covers the standard
#: web/retina ladder; the base width is multiplied by each to mint the responsive PNG set.
DEFAULT_WEB_SCALES: tuple[int, ...] = (1, 2, 3)

#: Print-profile Inkscape export options. These are REAL, verified Inkscape 1.4 flags
#: whose output is observably different from a plain PDF export — for ANY document, not just
#: text-bearing ones — and they are REPORTED in the result's `applied_settings` so the print value
#: is auditable:
#:   --export-pdf-version=1.4 — pins the produced PDF to version 1.4. Inkscape's DEFAULT (and the
#:                            plain `export_document` PDF) is 1.5, so this ALWAYS changes the output
#:                            bytes — the file header becomes `%PDF-1.4` vs the plain export's
#:                            `%PDF-1.5` (a deterministic, content-independent byte difference). 1.4
#:                            is also the more portable/conservative version for press handoff.
#:   --export-text-to-path  — converts any text to vector paths (no embedded/missing-font risk at
#:                            the press; font-independent print output). This additionally changes
#:                            the bytes whenever the document contains text.
#: They are appended verbatim by `export_document(..., _extra_args=...)`; this set is server-side
#: and never client-supplied (sec.12).
_PRINT_PDF_VERSION = "1.4"
_PRINT_EXTRA_ARGS: tuple[str, ...] = (
    "--export-text-to-path",
    f"--export-pdf-version={_PRINT_PDF_VERSION}",
)
_PRINT_APPLIED_SETTINGS: dict[str, str] = {
    "text_to_path": "true",
    "pdf_version": _PRINT_PDF_VERSION,
}


class ProfileArtifact(BaseModel):
    """One produced artifact within a profile run.

    ONE LOCATION CONTRACT: `path` and `workspace_relative_path` carry the SAME value — the
    file relative to the WORKSPACE ROOT (a managed output carries the
    `.inkscape-mcp/documents/<doc_id>/...` base) — so a caller opens the file by a single join to
    the workspace root with no `find`/`stat`. `path` is kept only for back-compat and now means
    exactly the same thing. `format` is the produced format token (png / pdf / svg).
    `width_px`/`height_px` are the TRUE on-disk raster dimensions for raster outputs and `None` for
    vector (PDF/SVG).
    `requested_size_px` is set only for icon-set entries and carries the square size that was
    requested. `scale` is set only for responsive-web entries produced via `scales=` and carries
    the density multiplier (e.g. 2 for the 2x asset). `requested_width_px` is set on every
    responsive-web PNG entry — including the `widths=` form, where there is no density `scale` — so
    a caller can always tell which requested width an entry corresponds to.
    """

    path: str
    workspace_relative_path: str
    format: str
    width_px: int | None
    height_px: int | None
    requested_size_px: int | None = None
    scale: int | None = None
    requested_width_px: int | None = None
    #: CONTENT-TRUTH, computed at produce time from the just-written artifact:
    #: raster (PNG) entries carry `opaque_px` (drawn non-transparent pixel count) + `all_blank`;
    #: PDF entries carry `is_vector` (no embedded raster image) + `fonts_outlined` (no embedded font
    #: — true vector when both hold). Each is None for entries it does not apply to / when skipped.
    opaque_px: int | None = None
    all_blank: bool | None = None
    is_vector: bool | None = None
    fonts_outlined: bool | None = None


class ProfileResult(BaseModel):
    """Outcome of one profile run: the profile token, its ordered artifacts, and applied settings.

    `applied_settings` records the print/profile-specific options that were applied
    (auditable) — empty for profiles that apply none.
    """

    doc_id: str
    profile: str
    artifacts: list[ProfileArtifact]
    applied_settings: dict[str, str] = Field(default_factory=dict)


class ProfileSizeError(Exception):
    """A requested icon size is non-positive or exceeds the configured pixel cap.

    Raised BEFORE any Inkscape invocation. The public message is stable and carries no host path.
    """


def _settings(settings: Settings | None) -> Settings:
    return settings if settings is not None else get_settings()


def _compose_prefix(caller_prefix: str | None, profile_key: str | None) -> str | None:
    """Join a caller-supplied `name_prefix` with the profile's own per-file key.

    The profile keys its files internally (e.g. `web-256w`, the icon size, `print`); a caller
    `name_prefix` is prepended so the produced filenames carry the caller's tag too. Either part may
    be None (web's SVG has no width key; a profile with no internal key); the result is the non-None
    parts joined by `-`, or None when both are absent. The composed value is still sanitized by the
    underlying engine's `_safe_name_fragment` before it reaches a filename (sec.12).
    """
    parts = [p for p in (caller_prefix, profile_key) if p]
    return "-".join(parts) if parts else None


def _artifact(
    result: RenderResult,
    *,
    requested_size_px: int | None = None,
    scale: int | None = None,
    requested_width_px: int | None = None,
) -> ProfileArtifact:
    """Adapt a CLI `RenderResult` into a `ProfileArtifact` (both resolvable paths preserved)."""
    return ProfileArtifact(
        path=result.artifact_path,
        workspace_relative_path=result.workspace_relative_path,
        format=result.format,
        width_px=result.width_px,
        height_px=result.height_px,
        requested_size_px=requested_size_px,
        scale=scale,
        requested_width_px=requested_width_px,
        opaque_px=result.opaque_px,
        all_blank=result.all_blank,
        is_vector=result.is_vector,
        fonts_outlined=result.fonts_outlined,
    )


def export_web_profile(
    doc_id: str,
    width_px: int = DEFAULT_WEB_WIDTH_PX,
    widths: list[int] | None = None,
    scales: list[int] | None = None,
    out_dir: str | None = None,
    name_prefix: str | None = None,
    settings: Settings | None = None,
) -> ProfileResult:
    """Produce the web profile: a responsive PNG set plus one plain SVG.

    The set of PNG widths is resolved in this order:
      - `widths` — an explicit list of pixel widths (each becomes one PNG);
      - else `scales` — density multipliers applied to `width_px` (e.g. [1,2,3] -> 1x/2x/3x);
      - else just `width_px` (the single-width back-compat default).
    Each PNG is pixel-capped before Inkscape runs (by the underlying engine), distinct on disk, and
    carries a resolvable location. The per-width stem (`web-<w>w`) keeps the responsive files
    distinguishable; an optional caller `name_prefix` is prepended to it. An optional
    `out_dir` (relative paths anchored to the workspace ROOT, then sandbox-validated;)
    targets a caller-chosen directory so a `dist/` tree can be assembled without a `Bash cp`. One
    plain SVG (vector) is always appended last. Returns resolvable artifact paths; the order is PNGs
    (ascending width) then the SVG.

    Raises `ProfileSizeError` if a resolved width or scale is non-positive; `SandboxViolation` for
    an out-of-workspace `out_dir`.
    """
    s = _settings(settings)
    resolved = _resolve_web_widths(width_px, widths, scales)

    artifacts: list[ProfileArtifact] = []
    for w, scale in resolved:
        png = export_document(
            doc_id,
            "png",
            width_px=w,
            out_dir=out_dir,
            name_prefix=_compose_prefix(name_prefix, f"web-{w}w"),
            settings=s,
        )
        artifacts.append(_artifact(png, scale=scale, requested_width_px=w))
    svg = export_document(
        doc_id,
        "svg",
        out_dir=out_dir,
        name_prefix=_compose_prefix(name_prefix, "web"),
        settings=s,
    )
    artifacts.append(_artifact(svg))
    _logger.info(
        "web profile exported",
        extra={"doc_id": doc_id, "widths": [w for w, _ in resolved]},
    )
    return ProfileResult(doc_id=doc_id, profile=WEB, artifacts=artifacts)


def _resolve_web_widths(
    width_px: int, widths: list[int] | None, scales: list[int] | None
) -> list[tuple[int, int | None]]:
    """Resolve the responsive `(width, scale)` set (sorted by width, de-duplicated).

    `widths` wins (scale is None — explicit widths carry no density meaning); else
    `scales * width_px` (scale recorded per entry); else `[(width_px, None)]`. A non-positive
    width or scale (or base `width_px`) raises `ProfileSizeError` before any Inkscape invocation.
    """
    if widths is not None:
        if not widths:
            raise ProfileSizeError("web profile requires at least one width")
        for w in widths:
            if w <= 0:
                raise ProfileSizeError("web width must be a positive integer")
        return [(w, None) for w in sorted(set(widths))]
    if scales is not None:
        if not scales:
            raise ProfileSizeError("web profile requires at least one scale")
        if width_px <= 0:
            raise ProfileSizeError("web width must be a positive integer")
        for sc in scales:
            if sc <= 0:
                raise ProfileSizeError("web scale must be a positive integer")
        by_width: dict[int, int] = {}
        for sc in sorted(set(scales)):
            by_width.setdefault(width_px * sc, sc)
        return [(w, by_width[w]) for w in sorted(by_width)]
    if width_px <= 0:
        raise ProfileSizeError("web width must be a positive integer")
    return [(width_px, None)]


def create_icon_set(
    doc_id: str,
    sizes: list[int] | None = None,
    out_dir: str | None = None,
    name_prefix: str | None = None,
    settings: Settings | None = None,
) -> ProfileResult:
    """Produce a multi-size square PNG icon set from the source document.

    Each requested size yields one PNG via `export_document(doc_id, "png", width_px=size)`. Sizes
    default to `DEFAULT_ICON_SIZES`. Every requested size is validated BEFORE any Inkscape
    invocation: a size `<= 0` or `> settings.max_export_px` raises `ProfileSizeError` and nothing
    is produced. An optional `out_dir` (relative paths anchored to the workspace ROOT, then
    sandbox-validated;) targets a caller-chosen dir and an optional `name_prefix`
    tags each file. The artifact order follows the requested (or default) size order.
    Returns workspace-relative artifact paths, one per size.

    Raises `SandboxViolation` for an out-of-workspace `out_dir`.
    """
    s = _settings(settings)
    requested = list(sizes) if sizes is not None else list(DEFAULT_ICON_SIZES)

    # Validate every size up front so an oversized/invalid request is rejected before Inkscape
    # is ever invoked (and so a partial icon set is never written).
    for size in requested:
        if size <= 0:
            raise ProfileSizeError("icon size must be a positive integer")
        if size > s.max_export_px:
            raise ProfileSizeError("icon size exceeds the configured pixel cap")

    artifacts: list[ProfileArtifact] = []
    for size in requested:
        result = export_document(
            doc_id,
            "png",
            width_px=size,
            out_dir=out_dir,
            name_prefix=_compose_prefix(name_prefix, f"icon-{size}"),
            settings=s,
        )
        artifacts.append(_artifact(result, requested_size_px=size))
    _logger.info("icon set exported", extra={"doc_id": doc_id, "sizes": requested})
    return ProfileResult(doc_id=doc_id, profile=ICON, artifacts=artifacts)


def export_print_profile(
    doc_id: str,
    out_dir: str | None = None,
    name_prefix: str | None = None,
    settings: Settings | None = None,
) -> ProfileResult:
    """Produce the print profile: a single page-area PDF with REAL print-specific settings.

    Unlike a plain `export_document(doc_id, "pdf")`, this applies print-oriented Inkscape export
    flags (`--export-pdf-version=1.4` + `--export-text-to-path`) so the produced bytes ALWAYS
    differ from a plain PDF export — even for a TEXT-FREE document. The plain export uses Inkscape's
    default PDF version 1.5, so pinning 1.4 deterministically changes the file (the header becomes
    `%PDF-1.4` vs `%PDF-1.5`) regardless of content; `--export-text-to-path` additionally outlines
    any text for a press-safe, font-independent result. The applied options are REPORTED in
    `applied_settings` so the print value is auditable. An optional `out_dir` (relative paths
    anchored to the workspace ROOT, then sandbox-validated;) targets a caller-chosen
    dir and an optional `name_prefix` tags the file. Returns a resolvable PDF path.

    Raises `SandboxViolation` for an out-of-workspace `out_dir`.
    """
    s = _settings(settings)
    pdf = export_document(
        doc_id,
        "pdf",
        out_dir=out_dir,
        name_prefix=_compose_prefix(name_prefix, "print"),
        _extra_args=list(_PRINT_EXTRA_ARGS),
        settings=s,
    )
    _logger.info(
        "print profile exported",
        extra={"doc_id": doc_id, "applied_settings": _PRINT_APPLIED_SETTINGS},
    )
    return ProfileResult(
        doc_id=doc_id,
        profile=PRINT,
        artifacts=[_artifact(pdf)],
        applied_settings=dict(_PRINT_APPLIED_SETTINGS),
    )
