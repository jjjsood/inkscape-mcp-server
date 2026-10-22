"""Inline raster attachment: embed a produced PNG in a tool result as an MCP ImageContent block.

Shared by the render/export tools so the agent perceives its output without a second `Read` of a
server-side artifact path. Bytes are always read from a SERVER-MINTED sandbox artifact path (never
client input, sec.12) and are gated by an `inline` flag, the raster-format allowlist and a byte
threshold, so an over-threshold export still produces the file and reports its path — it is just not
embedded.
"""

from __future__ import annotations

from pathlib import Path

from fastmcp.utilities.types import Image

from inkscape_mcp.config import get_settings
from inkscape_mcp.logging_setup import get_logger
from inkscape_mcp.registry import DocEntry, get_registry

_logger = get_logger("render.inline")

#: DEFAULT inline-image byte threshold. A produced raster is returned INLINE as an MCP
#: ImageContent block only when it is at or below this many bytes, so the agent sees its output
#: without a second `Read` of the artifact path. The default is deliberately small (≈5 MiB) — far
#: below the hard `Settings.max_output_bytes` artifact cap — because an inlined PNG is
#: base64-encoded into the MCP message; an over-threshold export still produces the file and
#: reports its path, it is just not embedded. A caller can raise/lower this per call via
#: `max_output_bytes`, or set `inline=False` to opt out of embedding entirely.
DEFAULT_INLINE_MAX_BYTES = 5 * 1024 * 1024

#: Raster formats whose artifact bytes can be embedded as an MCP ImageContent block. Vector outputs
#: (PDF/SVG) are never inlined — they are not an image content type — so they always return the bare
#: structured result with the artifact path.
_INLINE_FORMATS = frozenset({"png"})


def resolve_artifact_abspath(doc_id: str, workspace_relative_path: str) -> Path | None:
    """Resolve a SERVER-MINTED workspace-relative artifact path to its absolute on-disk path.

    sec.12: the path is the registry-owned `entry.root` joined to the value the render/export engine
    just produced (never client input), so the resolved file is always inside the sandbox. Returns
    None if the registry entry is gone or the file is missing (the caller then skips inlining rather
    than failing the whole call).
    """
    try:
        entry: DocEntry = get_registry().get(doc_id)
    except KeyError:  # pragma: no cover - the export just succeeded for this id
        return None
    candidate = Path(entry.root) / workspace_relative_path
    return candidate if candidate.is_file() else None


def inline_image(
    *,
    doc_id: str,
    workspace_relative_path: str,
    fmt: str,
    inline: bool,
    max_output_bytes: int | None,
) -> Image | None:
    """Build an MCP `Image` for a produced raster artifact, gated by `inline` + the byte threshold.

    Returns None (skip embedding) when: `inline` is False; the format is not an inlineable raster
    (PDF/SVG); the artifact cannot be resolved/read; or its on-disk size exceeds the effective
    threshold (`max_output_bytes` when given and > 0, else `DEFAULT_INLINE_MAX_BYTES`). The bytes
    are read from the SERVER-MINTED sandbox artifact path, never from client input (sec.12).
    """
    if not inline or fmt not in _INLINE_FORMATS:
        return None
    threshold = (
        max_output_bytes
        if (max_output_bytes is not None and max_output_bytes > 0)
        else (DEFAULT_INLINE_MAX_BYTES)
    )
    abspath = resolve_artifact_abspath(doc_id, workspace_relative_path)
    if abspath is None:
        return None
    try:
        size = abspath.stat().st_size
    except OSError:  # pragma: no cover - file existence just checked
        return None
    if size > threshold:
        _logger.info(
            "inline image skipped: over threshold",
            extra={"doc_id": doc_id, "size": size, "threshold": threshold},
        )
        return None
    # Defense in depth: never embed a file larger than the configured hard artifact cap either.
    if size > get_settings().max_output_bytes:
        return None
    try:
        data = abspath.read_bytes()
    except OSError:  # pragma: no cover
        return None
    return Image(data=data, format=fmt)
