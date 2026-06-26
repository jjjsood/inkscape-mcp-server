"""Per-document on-disk layout (workspace model).

All server-managed state lives under `<root>/.inkscape-mcp/`. Each opened document gets a
per-document folder keyed by its opaque id. These helpers construct the canonical paths and
create the directory tree as real directories only (never symlinks).
"""

from __future__ import annotations

from pathlib import Path

#: The single top-level state directory the server owns inside each workspace root.
STATE_DIR = ".inkscape-mcp"


def documents_dir(root: Path) -> Path:
    """`<root>/.inkscape-mcp/documents/`."""
    return root / STATE_DIR / "documents"


def doc_dir(root: Path, doc_id: str) -> Path:
    """`<root>/.inkscape-mcp/documents/<doc_id>/`."""
    return documents_dir(root) / doc_id


def original_copy(root: Path, doc_id: str) -> Path:
    """`<doc>/original.svg` — immutable byte-for-byte copy of the source."""
    return doc_dir(root, doc_id) / "original.svg"


def working_copy(root: Path, doc_id: str) -> Path:
    """`<doc>/working/document.svg` — the single live working copy."""
    return doc_dir(root, doc_id) / "working" / "document.svg"


def snapshots_dir(root: Path, doc_id: str) -> Path:
    """`<doc>/snapshots/`."""
    return doc_dir(root, doc_id) / "snapshots"


def snapshots_index(root: Path, doc_id: str) -> Path:
    """`<doc>/snapshots/index.json` — ordered snapshot manifest."""
    return snapshots_dir(root, doc_id) / "index.json"


def artifacts_dir(root: Path, doc_id: str) -> Path:
    """`<doc>/artifacts/`."""
    return doc_dir(root, doc_id) / "artifacts"


def exports_dir(root: Path, doc_id: str) -> Path:
    """`<doc>/artifacts/exports/`."""
    return artifacts_dir(root, doc_id) / "exports"


def operations_dir(root: Path, doc_id: str) -> Path:
    """`<doc>/operations/`."""
    return doc_dir(root, doc_id) / "operations"


def live_dir(root: Path) -> Path:
    """`<root>/.inkscape-mcp/live/` — root-scoped (not per-document) live-mode state.

    Live sessions have no `doc_id`, so live artifacts live here rather than under a document
    folder. Render outputs and sync staging go under this tree.
    """
    return root / STATE_DIR / "live"


def live_artifacts_dir(root: Path) -> Path:
    """`<root>/.inkscape-mcp/live/artifacts/` — rasterized live-canvas renders."""
    return live_dir(root) / "artifacts"


def live_operations_dir(root: Path) -> Path:
    """`<root>/.inkscape-mcp/live/operations/` — root-scoped Live Operation Records (E4-02).

    Live sessions have no registered `doc_id`, so a live mutation's record lives here rather
    than under a per-document `operations/` folder.
    """
    return live_dir(root) / "operations"


def action_maps_dir(root: Path) -> Path:
    """`<root>/.inkscape-mcp/action-maps/` — root-scoped versioned Action capability maps (E6-02).

    The Action surface is host-wide (not per document), so the version-keyed capability maps live
    here rather than under a document folder. One file per detected Inkscape version.
    """
    return root / STATE_DIR / "action-maps"


def ensure_action_maps_dir(root: Path) -> None:
    """Create the root-scoped action-maps directory (real dir only, never a symlink). Idempotent."""
    action_maps_dir(root).mkdir(parents=True, exist_ok=True)


def ensure_live_dirs(root: Path) -> None:
    """Create the root-scoped live-mode directory tree (real dirs only). Idempotent."""
    for path in (live_dir(root), live_artifacts_dir(root), live_operations_dir(root)):
        path.mkdir(parents=True, exist_ok=True)


def ensure_doc_dirs(root: Path, doc_id: str) -> None:
    """Create the per-document directory tree (real dirs only, never symlinks).

    Idempotent. Creates the doc folder plus `working/`, `snapshots/`, `artifacts/exports/`,
    and `operations/`.
    """
    for path in (
        doc_dir(root, doc_id),
        working_copy(root, doc_id).parent,
        snapshots_dir(root, doc_id),
        artifacts_dir(root, doc_id),
        exports_dir(root, doc_id),
        operations_dir(root, doc_id),
    ):
        path.mkdir(parents=True, exist_ok=True)
