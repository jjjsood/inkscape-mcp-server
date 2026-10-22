"""Inline raster attachment: embed a produced PNG in a tool result as an MCP ImageContent block.

Shared by the render/export tools so the agent perceives its output without a second `Read` of a
server-side artifact path. Bytes are always read from a SERVER-MINTED sandbox artifact path (never
client input, sec.12) and are gated by an `inline` flag, the raster-format allowlist and a byte
threshold, so an over-threshold export still produces the file and reports its path — it is just not
embedded.
"""

from __future__ import annotations

import json
from pathlib import Path

import mcp.types as mcp_types
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image
from pydantic import BaseModel

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


#: Local-only artifact path fields scrubbed from the human/agent-facing TEXT payload when the
#: raster is attached inline: they are workspace-root-relative (sec.12, never absolute), so a client
#: that tries to `Read` them resolves against its own CWD (!= the server workspace root) and fails.
#: The full values stay in `structured_content` for machine consumers and schema validation.
_INLINE_SCRUBBED_PATH_FIELDS = ("artifact_path", "workspace_relative_path")

#: Appended to the inline text payload so the agent trusts the attached image instead of chasing a
#: path it cannot resolve.
_INLINE_NOTE = (
    "Raster attached inline as an image — view it directly. The artifact is stored server-side "
    "at a workspace-relative path (see structured output); it is NOT on your local filesystem, "
    "so do not Read it."
)


def with_inline[ModelT: BaseModel](result: ModelT, image: Image | None) -> ModelT | ToolResult:
    """Return the bare structured `result`, or a `ToolResult` carrying it PLUS the inline image.

    When `image` is None the bare Pydantic model is returned unchanged (existing direct callers and
    structured-output consumers see exactly the same shape as before — is additive). When an
    image is present the inline raster is appended as an MCP ImageContent block (so the agent
    perceives its output without a second `Read`), the full structured result is preserved in
    `structured_content` (so `.artifact_path` / dims / `stale` remain machine-readable), and the
    human-facing TEXT payload is built WITHOUT the workspace-relative path fields — replaced by a
    short note — so a path-chasing agent is not lured into a `Read` of a server-side path it cannot
    resolve (1B).
    """
    if image is None:
        return result
    structured = result.model_dump()
    display = {k: v for k, v in structured.items() if k not in _INLINE_SCRUBBED_PATH_FIELDS}
    display["note"] = _INLINE_NOTE
    return ToolResult(
        content=[
            mcp_types.TextContent(type="text", text=json.dumps(display)),
            image.to_image_content(),
        ],
        structured_content=structured,
    )
