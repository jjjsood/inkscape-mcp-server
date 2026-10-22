"""Read-only artifact stat: on-disk byte size + sha256 of a sandboxed artifact.

Lets agents read back what they wrote without shelling out to `wc -c` / `sha256sum` / `du -cb`.
Paths may be workspace-relative (anchored to the first workspace root) or absolute; either is
sandbox-validated, and the returned `path` is always workspace-relative (never a host path, sec.12).
Failures surface as `SandboxViolation` / `LimitExceeded` for the caller to map.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel

from inkscape_mcp.config import get_settings
from inkscape_mcp.workspace.limits import check_input_size
from inkscape_mcp.workspace.paths import anchor_to_root, owning_root, resolve_read_path

#: Chunk size for the streaming sha256 read. Bounds peak memory so hashing a large (but
#: under-limit) artifact never loads the whole file at once (sec.12 memory hygiene).
_HASH_CHUNK_BYTES = 1024 * 1024


class ArtifactStat(BaseModel):
    """On-disk facts about one sandboxed artifact.

    `path` echoes the WORKSPACE-RELATIVE POSIX path of the resolved artifact (never a host path —
    sec.12); `bytes` is its raw on-disk size; `sha256` is the lowercase hex digest of its contents.
    """

    path: str
    bytes: int
    sha256: str


class ArtifactStatSet(BaseModel):
    """Per-file stat plus an aggregate byte total for a SET of artifacts.

    `artifacts` carries one `ArtifactStat` per input path (order preserved); `total_bytes` is the
    sum of their `bytes` — the icon-set / dist-tree byte budget without a `du -cb`. `count` is the
    number of artifacts stat'd.
    """

    artifacts: list[ArtifactStat]
    total_bytes: int
    count: int


def stat_one(raw_path: str) -> ArtifactStat:
    """Resolve `raw_path` through the read-path sandbox and return its size + streamed sha256.

    The path may be workspace-RELATIVE (anchored to the first workspace root, matching
    `open_document` / `save_document_as`) or absolute; either is sandbox-validated and a
    `../`-escape, an absolute path outside the workspace, or a symlink whose target leaves the
    sandbox is rejected with `path rejected: outside workspace` (sec.12). Size is enforced against
    `max_input_bytes` and the digest is computed STREAMING in bounded chunks so a large (but
    under-limit) artifact never loads whole into memory. The returned `path` is workspace-relative
    (no host path leaks). Raises `SandboxViolation` / `LimitExceeded` for the caller to map.
    """
    settings = get_settings()
    anchored = anchor_to_root(raw_path, settings)
    resolved = resolve_read_path(anchored, settings)

    # Enforce the input-size cap BEFORE hashing (sec.12) — never hash an over-limit file.
    size = check_input_size(resolved, settings)

    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)

    return ArtifactStat(
        path=_workspace_relative(resolved, settings.workspace_roots),
        bytes=size,
        sha256=digest.hexdigest(),
    )


def _workspace_relative(resolved: Path, roots: list[Path]) -> str:
    """Return `resolved` as a POSIX path relative to its owning workspace root (no host path).

    The path is already proven contained by `resolve_read_path`, so an owning root always exists;
    the relative form is the only value handed back to the client (sec.12, no host-path leak).
    """
    root = owning_root(resolved, roots)
    if root is None:  # pragma: no cover - resolve_read_path proved containment
        return resolved.name
    return resolved.relative_to(root).as_posix()


def stat_many(raw_paths: list[str]) -> ArtifactStatSet:
    """Stat every path in `raw_paths` (order preserved) and return the per-file stats + total.

    The first path that escapes the sandbox or exceeds the size limit raises (`SandboxViolation` /
    `LimitExceeded`), so nothing partial is returned.
    """
    stats = [stat_one(raw) for raw in raw_paths]
    return ArtifactStatSet(
        artifacts=stats, total_bytes=sum(s.bytes for s in stats), count=len(stats)
    )
