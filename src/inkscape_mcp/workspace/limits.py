"""Size / dimension limits (workspace model).

Limits are checked BEFORE the work starts; a request that would exceed a limit is rejected
cleanly (no partial work). Input size is checked on the raw bytes before any parse.
"""

from __future__ import annotations

from pathlib import Path

from inkscape_mcp.config import Settings, get_settings


class LimitExceeded(Exception):
    """A request exceeds a configured workspace limit."""


def _settings(settings: Settings | None) -> Settings:
    return settings if settings is not None else get_settings()


def check_input_size(path: Path, settings: Settings | None = None) -> int:
    """Stat the raw byte size of `path` (before any parse) and enforce `max_input_bytes`.

    Returns the size in bytes. Raises `LimitExceeded` if it exceeds the cap.
    """
    s = _settings(settings)
    size = path.stat().st_size
    if size > s.max_input_bytes:
        raise LimitExceeded(f"input file exceeds max size: {size} > {s.max_input_bytes} bytes")
    return size


def check_input_bytes_size(data: bytes, settings: Settings | None = None) -> int:
    """Enforce `max_input_bytes` on IN-MEMORY SVG bytes (no file to stat).

    The byte-string counterpart of :func:`check_input_size` for content that never lands on disk
    first (E14-02 `create_document`, E14-03 `set_document_svg`/`insert_svg_fragment` adopt an SVG
    string). Returns the size in bytes. Raises `LimitExceeded` if it exceeds the cap.
    """
    s = _settings(settings)
    size = len(data)
    if size > s.max_input_bytes:
        raise LimitExceeded(f"input exceeds max size: {size} > {s.max_input_bytes} bytes")
    return size


def check_export_dimensions(
    width_px: int, height_px: int, settings: Settings | None = None
) -> None:
    """Enforce `max_export_px` on both raster export dimensions.

    Raises `LimitExceeded` if either dimension exceeds the cap.
    """
    s = _settings(settings)
    if width_px > s.max_export_px or height_px > s.max_export_px:
        raise LimitExceeded(
            f"export dimensions exceed cap: {width_px}x{height_px} > {s.max_export_px}px per side"
        )


def check_output_size(path: Path, settings: Settings | None = None) -> None:
    """Enforce `max_output_bytes` on a produced artifact.

    Raises `LimitExceeded` if the produced file exceeds the cap.
    """
    s = _settings(settings)
    size = path.stat().st_size
    if size > s.max_output_bytes:
        raise LimitExceeded(f"output file exceeds max size: {size} > {s.max_output_bytes} bytes")
