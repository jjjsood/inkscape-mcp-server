"""Document id ↔ path registry (workspace model).

`open_document` validates the source path through the §3 sandbox check, enforces the input
size cap, mints a fresh OPAQUE id (`d_` + random token, never derivable into a path), creates
the per-document workspace, copies the source byte-for-byte into `original.svg`, seeds
`working/document.svg` from it, and persists the id↔path mapping to
`<root>/.inkscape-mcp/registry.json` with the source path stored RELATIVE to its owning root.

The resolved real path is the only value passed to copy (TOCTOU, §3).
"""

from __future__ import annotations

import json
import secrets
import shutil
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from inkscape_mcp.config import Settings, get_settings
from inkscape_mcp.logging_setup import get_logger, log_file_io
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.limits import check_input_bytes_size, check_input_size
from inkscape_mcp.workspace.paths import anchor_to_root, owning_root, resolve_read_path

_logger = get_logger("registry")

REGISTRY_FILENAME = "registry.json"


class DocEntry(BaseModel):
    """One registered document: opaque id plus its workspace locations.

    `source_path` is stored RELATIVE to `root` (for restart re-attach); `root`,
    `workspace_dir`, `original_path`, and `working_path` are absolute real paths for runtime
    use. IDs are opaque and never embed a host path.
    """

    doc_id: str
    source_path: str
    root: str
    workspace_dir: str
    opened_at: str
    original_path: str
    working_path: str


def _registry_path(root: Path) -> Path:
    return root / sandbox.STATE_DIR / REGISTRY_FILENAME


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def _mint_doc_id() -> str:
    return f"d_{secrets.token_hex(4)}"


class Registry:
    """In-process document id ↔ path map, persisted per workspace root."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings if settings is not None else get_settings()
        self._docs: dict[str, DocEntry] = {}

    def open_document(self, source_path: str | Path) -> DocEntry:
        """Validate, copy, register, and persist a freshly opened document.

        Steps (in order): anchor a RELATIVE `source_path` to the first workspace root → §3
        read-path validation → input-size check → pick owning root → mint opaque id → create dir
        tree → byte-for-byte copy to `original.svg` → seed `working/document.svg` → register +
        persist. Returns the `DocEntry`.

        A relative `source_path` is anchored to `workspace_roots[0]` (the document root, NOT the
        process CWD) before validation — matching the sibling write APIs `save_document_as` /
        `live_sync_to_workspace` and the one-location contract. An absolute path is
        passed through unchanged. The anchor only chooses the base; `resolve_read_path` remains the
        sole containment + symlink authority, so a relative `../`-escape, an absolute path outside
        the workspace, or a symlink whose target leaves the sandbox is still rejected with
        `path rejected: outside workspace` — the escape surface is not widened.
        """
        anchored = anchor_to_root(source_path, self._settings)
        resolved = resolve_read_path(anchored, self._settings)
        check_input_size(resolved, self._settings)

        root = owning_root(resolved, self._settings.workspace_roots)
        if root is None:  # pragma: no cover - resolve_read_path already guarantees containment
            raise ValueError("source path resolved outside all workspace roots")

        doc_id = _mint_doc_id()
        while doc_id in self._docs:  # pragma: no cover - collision astronomically unlikely
            doc_id = _mint_doc_id()

        sandbox.ensure_doc_dirs(root, doc_id)
        original = sandbox.original_copy(root, doc_id)
        working = sandbox.working_copy(root, doc_id)

        # The resolved real path is the only value handed to copy (TOCTOU, §3).
        shutil.copyfile(resolved, original)
        shutil.copyfile(original, working)
        log_file_io(
            _logger,
            action="open_document",
            doc_id=doc_id,
            original=str(original),
            working=str(working),
        )

        try:
            rel_source = str(resolved.relative_to(root))
        except ValueError:  # pragma: no cover - containment already guarantees this holds
            rel_source = resolved.name

        entry = DocEntry(
            doc_id=doc_id,
            source_path=rel_source,
            root=str(root),
            workspace_dir=str(sandbox.doc_dir(root, doc_id)),
            opened_at=_utc_iso(),
            original_path=str(original),
            working_path=str(working),
        )
        self._docs[doc_id] = entry
        self._persist(root)
        return entry

    def create_document(self, svg_bytes: bytes) -> DocEntry:
        """Register a freshly SEEDED document from in-memory SVG bytes.

        Unlike :meth:`open_document` there is NO external source file: the supplied `svg_bytes`
        (a server-generated blank SVG, already safe-parsed by the caller) are written byte-for-byte
        into BOTH `original.svg` (the immutable baseline / reload seed) and the live
        `working/document.svg`. The doc is otherwise identical to an opened one — same opaque id,
        same dir tree, same persistence — so every downstream tool (inspect / edit / snapshot /
        reload) treats it the same.

        A created document has NO external source, so its `source_path` is the conventional name
        `document.svg` (relative, never a host path). `reload` of such a doc restores from
        `original.svg` (the blank seed). The bytes are size-checked against the input cap before
        anything is written. Returns the `DocEntry`.
        """
        check_input_bytes_size(svg_bytes, self._settings)

        root = self._settings.workspace_roots[0] if self._settings.workspace_roots else None
        if root is None:  # pragma: no cover - resolve helpers already guarantee a configured root
            raise ValueError("no workspace root configured")

        doc_id = _mint_doc_id()
        while doc_id in self._docs:  # pragma: no cover - collision astronomically unlikely
            doc_id = _mint_doc_id()

        sandbox.ensure_doc_dirs(root, doc_id)
        original = sandbox.original_copy(root, doc_id)
        working = sandbox.working_copy(root, doc_id)

        # Seed BOTH original.svg (immutable baseline / reload seed) and the working copy from the
        # SAME server-generated bytes — never from a client path (no source file exists).
        original.write_bytes(svg_bytes)
        shutil.copyfile(original, working)
        log_file_io(
            _logger,
            action="create_document",
            doc_id=doc_id,
            original=str(original),
            working=str(working),
        )

        entry = DocEntry(
            doc_id=doc_id,
            source_path="document.svg",
            root=str(root),
            workspace_dir=str(sandbox.doc_dir(root, doc_id)),
            opened_at=_utc_iso(),
            original_path=str(original),
            working_path=str(working),
        )
        self._docs[doc_id] = entry
        self._persist(root)
        return entry

    def reload(self, doc_id: str) -> DocEntry:
        """Refresh a working copy FROM ITS SOURCE under the SAME `doc_id`.

        Re-copies `original.svg` over `working/document.svg`, discarding any working-copy edits and
        returning the document to its source bytes. For an OPENED document the stored `source_path`
        is re-resolved through the §3 sandbox check and re-validated as still inside the sandbox
        BEFORE the copy (a source that has since moved out of the workspace, or vanished, is
        rejected with a `SandboxViolation`); the re-validated source is then re-copied into BOTH
        `original.svg` and the working copy so the baseline tracks the current source. For a CREATED
        document (`create_document`, no external source) there is nothing to re-resolve, so the
        reload restores from `original.svg` (the blank seed) — the documented safe behaviour.

        The CALLER (the tool layer) is responsible for taking a PRE-reload snapshot before invoking
        this so the reload is itself reversible; this method only re-copies. The size cap is
        enforced on the source before the copy. Raises `KeyError` for an unknown `doc_id` and
        `SandboxViolation` if a (moved) external source no longer resolves inside the sandbox.
        """
        entry = self._docs[doc_id]
        root = Path(entry.root)
        original = sandbox.original_copy(root, doc_id)
        working = sandbox.working_copy(root, doc_id)

        external_source = root / entry.source_path
        # A created doc's seed lives only in original.svg; re-resolve+re-copy an EXTERNAL source if
        # one still exists inside the sandbox, otherwise fall back to the original.svg baseline.
        if entry.source_path != "document.svg" and external_source.is_file():
            resolved = resolve_read_path(external_source, self._settings)
            check_input_size(resolved, self._settings)
            # Refresh the immutable baseline AND the working copy from the current source bytes.
            shutil.copyfile(resolved, original)
        else:
            check_input_size(original, self._settings)
        shutil.copyfile(original, working)
        log_file_io(
            _logger,
            action="reload_document",
            doc_id=doc_id,
            original=str(original),
            working=str(working),
        )
        return entry

    def get(self, doc_id: str) -> DocEntry:
        """Return the entry for `doc_id`; raise `KeyError` if unknown."""
        return self._docs[doc_id]

    def list_documents(self) -> list[DocEntry]:
        """Return all registered document entries."""
        return list(self._docs.values())

    def _persist(self, root: Path) -> None:
        """Write all entries belonging to `root` to that root's `registry.json`.

        The persisted source path is RELATIVE to the root (never an absolute host path).
        """
        entries = [
            {
                "doc_id": e.doc_id,
                "source_path": e.source_path,
                "workspace_dir_name": Path(e.workspace_dir).name,
                "opened_at": e.opened_at,
            }
            for e in self._docs.values()
            if e.root == str(root)
        ]
        target = _registry_path(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"documents": entries}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


_registry_singleton: Registry | None = None


def get_registry() -> Registry:
    """Return the process-wide `Registry` singleton (reset for tests via `reset_registry`)."""
    global _registry_singleton
    if _registry_singleton is None:
        _registry_singleton = Registry()
    return _registry_singleton


def reset_registry() -> None:
    """Drop the cached singleton (test helper)."""
    global _registry_singleton
    _registry_singleton = None
