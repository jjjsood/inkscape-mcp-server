"""Inline raster attachment: embed a produced PNG in a tool result as an MCP ImageContent block.

Shared by the render/export tools so the agent perceives its output without a second `Read` of a
server-side artifact path. Bytes are always read from a SERVER-MINTED sandbox artifact path (never
client input, sec.12) and are gated by a byte threshold, so an over-threshold export still produces
the file and reports its path — it is just not embedded.
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
#: reports its path, it is just not embedded.
DEFAULT_INLINE_MAX_BYTES = 5 * 1024 * 1024


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


def png_image(abspath: Path | None) -> Image | None:
    """An MCP `Image` for a produced PNG artifact, or None when it must not be embedded.

    Returns None when `abspath` is None/unreadable or its on-disk size exceeds
    `min(DEFAULT_INLINE_MAX_BYTES, Settings.max_output_bytes)`. The caller passes a SERVER-MINTED
    sandbox path, never client input (sec.12).
    """
    if abspath is None:
        return None
    limit = min(DEFAULT_INLINE_MAX_BYTES, get_settings().max_output_bytes)
    try:
        size = abspath.stat().st_size
        if size > limit:
            _logger.info(
                "inline image skipped: over threshold", extra={"size": size, "threshold": limit}
            )
            return None
        return Image(data=abspath.read_bytes(), format="png")
    except OSError:
        return None
