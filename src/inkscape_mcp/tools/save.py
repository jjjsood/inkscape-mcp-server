"""Save tool (E2-05): `save_document_as` + pre/post validation.

Persists the current working-copy state of a registered document to a NEW file inside the
workspace sandbox, running `validate_document` (E1-08) both before and after the write. The
original/source file is never overwritten; overwriting an EXISTING file on disk requires an
explicit `approval_token` and escalates the operation to HIGH risk (sec.12). Every save opens
an Operation Record (ADR-004) so the persisted change is auditable.

Path safety (sec.12): a RELATIVE `dest_path` is anchored to the first configured workspace
root (NOT the server CWD) before validation; an absolute dest must already resolve inside a
root. When the destination names a SUBFOLDER that does not exist yet, the missing parent
directories are CREATED first — but only after a TOCTOU-safe containment proof: the longest
existing ancestor is resolved (symlinks followed) and `commonpath`-checked against the
configured roots BEFORE any side-effect, then the missing tail is created relative to a
directory file descriptor on that validated ancestor, descending with `O_NOFOLLOW` so a symlink
raced into the path cannot redirect the `mkdir` outside the sandbox (mirrors the proven
`render/cli.py::_resolve_out_dir` pattern). A `..`-escaping or absolute-outside dest still
creates nothing and is rejected with `path rejected: outside workspace`. The destination is
then validated through the §3 write-path choke point (`resolve_write_path`), which resolves the
parent strictly (following symlinks), containment-checks it, and — when the final component
already exists as a symlink — rejects a link whose real target leaves the sandbox. That resolved
real path is the ONLY value handed to I/O (TOCTOU hardening), and the copy itself refuses to
follow a symlinked destination (`O_NOFOLLOW`) as defence in depth. The tool also refuses,
independently of the `overwrite` flag, to write over any managed document file (an
`original.svg`, a `working/document.svg`, or a registered source path).

All client-facing errors are raised as `ToolError` with stable, host-path-free messages.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

from fastmcp.exceptions import ToolError
from pydantic import BaseModel

from inkscape_mcp.config import get_settings
from inkscape_mcp.document.inspect import DocumentNotFound, InspectionError
from inkscape_mcp.logging_setup import get_logger, log_file_io, log_tool_call
from inkscape_mcp.operations import (
    OperationStatus,
    new_operation,
    update_operation,
)
from inkscape_mcp.registry import DocEntry, Registry, get_registry
from inkscape_mcp.server import mcp
from inkscape_mcp.validate import ValidationReport, validate_document
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.paths import (
    SandboxViolation,
    is_contained,
    owning_root,
    resolve_write_path,
)
from inkscape_mcp.workspace.risk import PolicyViolation, RiskClass

_logger = get_logger("tools.save")


class SaveResult(BaseModel):
    """Result of `save_document_as`.

    `saved_path` is a WORKSPACE-RELATIVE POSIX path (relative to the document's owning
    workspace root), never an absolute host path, so it can be returned to a client without
    leaking the on-disk layout. `overwritten` is True iff the destination already existed on
    disk before the save (a HIGH-risk, approval-gated overwrite). `pre_validation` and
    `post_validation` are the `validate_document` reports from before and after the write.
    """

    doc_id: str
    saved_path: str
    operation_id: str
    overwritten: bool
    pre_validation: ValidationReport
    post_validation: ValidationReport


def _managed_paths(registry: Registry) -> set[Path]:
    """Collect every managed document file path that must never be overwritten.

    For each registered document this is its `original.svg`, its `working/document.svg`, and
    its registered source file (reconstructed from the owning root + stored relative source).
    Paths are resolved where they exist so a comparison is done on canonical real paths.
    """
    protected: set[Path] = set()
    for entry in registry.list_documents():
        for raw in (entry.original_path, entry.working_path):
            p = Path(raw)
            protected.add(_safe_resolve(p))
        source = Path(entry.root) / entry.source_path
        protected.add(_safe_resolve(source))
    return protected


def _safe_resolve(p: Path) -> Path:
    """Resolve `p` to a canonical real path, tolerating a not-yet-existing target.

    Resolves the parent strictly and re-attaches the final component so an existing file and
    a freshly resolved destination compare equal. If the final component itself exists as a
    symlink it is followed to its real target, so a symlink planted at a managed-file name is
    still recognised as that managed file (and a symlinked destination resolves to what it
    actually points at, not to the link path). Mirrors the comparison basis used by
    `resolve_write_path`.
    """
    try:
        base = p.parent.resolve(strict=True) / p.name
    except (OSError, RuntimeError):
        return p
    if base.is_symlink():
        try:
            return base.resolve(strict=False)
        except (OSError, RuntimeError):
            return base
    return base


def _relative_to_root(resolved_dest: Path, entry: DocEntry) -> str:
    """Return the destination as a POSIX path relative to its owning workspace root.

    Prefers the destination's own owning root (the configured root that contains it); falls
    back to the document's registered root. The result never contains an absolute host path.
    """
    base = owning_root(resolved_dest, get_settings().workspace_roots) or Path(entry.root)
    try:
        return resolved_dest.relative_to(base).as_posix()
    except ValueError:  # pragma: no cover - containment guarantees this holds
        return resolved_dest.name


def _anchor_dest(dest_path: str) -> str:
    """Anchor a RELATIVE destination to the workspace root, leaving absolute dests untouched.

    A relative `dest_path` (the common, friendly form) resolves against the FIRST configured
    workspace root — the document root — so a save never lands relative to the server's process
    CWD (E10-08 SV1). An absolute dest is returned unchanged; `resolve_write_path` then enforces
    that it still lands inside a configured root. If no workspace root is configured the raw
    value is returned and the sandbox choke point rejects it cleanly.
    """
    candidate = Path(dest_path)
    if candidate.is_absolute():
        return dest_path
    roots = get_settings().workspace_roots
    if not roots:
        return dest_path
    return str(roots[0] / candidate)


def _ensure_parent_dir(anchored_dest: str) -> None:
    """Create the destination's parent directory tree if missing — TOCTOU-safe (sec.12).

    `anchored_dest` is the dest already anchored to the workspace root (relative dests resolved,
    absolute dests untouched). When the parent already exists this is a no-op; when one or more
    leading components are missing they are created so an agent can naturally save into a fresh
    subfolder. Containment is proven BEFORE any side-effect, mirroring the audited pattern in
    `render/cli.py::_resolve_out_dir`/`_safe_mkdir_chain`:

    1. A literal `..` anywhere in the destination is refused outright (`path rejected: outside
       workspace`) — no directory is created.
    2. The longest EXISTING ancestor of the parent is resolved with `strict=True` (every symlink
       followed) and `commonpath`-checked against the configured roots. An ancestor that resolves
       outside every root is rejected before anything is created, so a `../`- or absolute-escape
       can never plant a directory outside the sandbox.
    3. The missing tail is then created one component at a time relative to a directory file
       descriptor opened on that validated-contained ancestor, descending with
       `O_RDONLY|O_DIRECTORY|O_NOFOLLOW`; if any component is (or is raced into) a symlink the
       descend `open` fails with `ELOOP` and creation aborts — the side-effect can never escape
       the validated ancestor.

    No host path appears in any raised message. The final-component symlink guard and the
    `O_NOFOLLOW` write in `save_document_as` stay the authority on the destination itself; this
    helper only ever creates the PARENT tree.
    """
    settings = get_settings()
    if not settings.workspace_roots:
        # No root configured: leave the dest for the sandbox choke point to reject cleanly.
        return
    parent = Path(anchored_dest).parent
    if any(part == ".." for part in Path(anchored_dest).parts):
        raise SandboxViolation(
            "path rejected: outside workspace",
            detail=f"dest_path {anchored_dest!r} contains a '..' component",
        )

    # Longest existing ancestor (PRE-CREATE CONTAINMENT): resolve + commonpath-check it BEFORE
    # creating anything (sec.12), so an escape never reaches the mkdir side-effect.
    existing = parent
    while not existing.exists():
        nxt = existing.parent
        if nxt == existing:  # reached the filesystem root
            break
        existing = nxt
    try:
        resolved_existing = existing.resolve(strict=True)
    except OSError as exc:
        raise SandboxViolation(
            "path rejected: could not resolve path",
            detail=f"dest parent prefix resolve failed: {exc}",
        ) from None
    if not is_contained(resolved_existing, settings.workspace_roots):
        raise SandboxViolation(
            "path rejected: outside workspace",
            detail=f"dest_path {anchored_dest!r} parent resolves outside all configured roots",
        )

    missing = parent.relative_to(existing).parts
    if missing:
        _safe_mkdir_chain(resolved_existing, missing)


def _safe_mkdir_chain(base_real_dir: Path, components: tuple[str, ...]) -> None:
    """Create `components` under `base_real_dir` one level at a time, TOCTOU-safe (sec.12).

    `base_real_dir` MUST already be a resolved, sandbox-contained real directory. Each component
    is created with `os.mkdir(..., dir_fd=...)` and then descended with
    `O_RDONLY|O_DIRECTORY|O_NOFOLLOW`, so if any component is (or is raced into) a symlink the
    descend `open` fails with `ELOOP`/`ENOTDIR` and creation aborts — the side-effect can never
    escape the base dir. Mirrors `render/cli.py::_safe_mkdir_chain`.
    """
    dir_fd = os.open(base_real_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in components:
            if part in ("", ".", ".."):
                raise SandboxViolation(
                    "path rejected: invalid filename",
                    detail=f"unsafe dest_path component {part!r}",
                )
            try:
                os.mkdir(part, dir_fd=dir_fd)
            except FileExistsError:
                pass
            try:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            except OSError as exc:  # ELOOP (symlink) / ENOTDIR — refuse, never follow
                raise SandboxViolation(
                    "path rejected: outside workspace",
                    detail=f"dest_path component {part!r} is not a real directory: {exc}",
                ) from None
            # Reassign BEFORE closing so `finally` always closes the live fd even if close() raises.
            old_fd, dir_fd = dir_fd, next_fd
            os.close(old_fd)
    finally:
        os.close(dir_fd)


@mcp.tool
def save_document_as(
    doc_id: str,
    dest_path: str,
    overwrite: bool = False,
    approval_token: str | None = None,
) -> SaveResult:
    """Save a document's current working-copy state to a NEW file in the workspace.

    When to use: persisting the working copy to disk. To export a raster/PDF instead use
    `export_document`; to snapshot in-server state (not a file) use `create_snapshot`. The original
    and source files are never touched.

    Key params: `dest_path` may be RELATIVE or absolute — relative anchors to the FIRST configured
    workspace root (NOT the server CWD); absolute must resolve inside a configured root. A dest into
    a not-yet-existing SUBFOLDER (e.g. `"output/final.svg"`) is supported: missing parents are
    created only after proving they resolve INSIDE the workspace (a `..`-escaping / out-of-sandbox
    dest creates nothing and is rejected with `path rejected: outside workspace`). The dest is
    sandbox- and symlink-checked (incl. a pre-existing symlink at the final name) and the copy never
    follows a symlinked dest (sec.12). Overwriting an existing file requires `overwrite=True` PLUS a
    non-empty `approval_token`.

    Return shape: `SaveResult` — `saved_path` (workspace-relative POSIX), `operation_id`,
    `overwritten`, and `pre_validation` / `post_validation` (the `validate_document` reports from
    before and after the write).

    Example: `save_document_as(doc_id, "output/final.svg")`

    Risk class: medium for a new-file save; high (approval-gated) when overwriting an existing file.
    """
    registry = get_registry()

    # 1. PRE validation (also surfaces an unknown / unparseable document early).
    try:
        pre_report = validate_document(doc_id, registry=registry)
    except (DocumentNotFound, KeyError) as exc:
        raise ToolError("document id not found") from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc

    entry = registry.get(doc_id)

    # 2. Anchor a RELATIVE dest to the workspace root (E10-08 SV1), NOT the server CWD, then
    #    (when the dest names a not-yet-existing subfolder) create the parent tree under a
    #    pre-create containment proof (sec.12), and finally resolve through the sandbox write-path
    #    choke point (§3). An absolute dest is passed through unchanged and must itself resolve
    #    inside a configured root. A containment failure in EITHER step raises SandboxViolation,
    #    which is mapped to the safe, host-path-free ToolError below — and the parent-creation
    #    step proves containment BEFORE any mkdir, so a rejected dest creates nothing.
    anchored_dest = _anchor_dest(dest_path)
    try:
        _ensure_parent_dir(anchored_dest)
        resolved_dest = resolve_write_path(anchored_dest)
    except SandboxViolation as exc:
        _logger.error("save_document_as rejected", extra={"detail": exc.detail})
        # Use exc.args[0] explicitly: it is the SAFE public message (no host path). `str(exc)`
        # would happen to resolve to the same value today, but reading args[0] keeps the safe
        # field pinned even if SandboxViolation's str form ever changes.
        raise ToolError(exc.args[0]) from exc

    # 3. Never overwrite a managed document file (independent of the `overwrite` flag).
    if _safe_resolve(resolved_dest) in _managed_paths(registry):
        raise ToolError("cannot overwrite a managed document file")

    # 4. Refuse to write THROUGH a symlink at the destination, full stop. `resolve_write_path`
    #    has already rejected a symlink whose real target leaves the sandbox; an IN-sandbox link
    #    that reaches here is still refused (we never overwrite via a link, and this gives a clear
    #    message instead of the later O_NOFOLLOW ELOOP failure).
    if resolved_dest.is_symlink():
        raise ToolError("cannot overwrite a symbolic link")

    # 5. An already-existing destination is an overwrite → HIGH risk, approval-gated.
    overwritten = resolved_dest.exists()
    if overwritten:
        if not overwrite or not approval_token:
            raise ToolError(
                "destination already exists; overwriting requires overwrite=True and an "
                "approval_token"
            )
        risk_class = RiskClass.HIGH
    else:
        risk_class = RiskClass.MEDIUM

    # 6. Open the Operation Record (let `new_operation` enforce the risk policy).
    try:
        record = new_operation(
            doc_id,
            tool="save_document_as",
            risk_class=risk_class,
            params={"dest_path": dest_path, "overwrite": overwrite},
            registry=registry,
            approval_token=approval_token,
        )
    except PolicyViolation as exc:
        raise ToolError(str(exc)) from exc

    # 7. Write the current working-copy bytes to the resolved destination (internal path only).
    #    Use an O_NOFOLLOW open so the copy can NEVER follow a symlink at the final destination
    #    name (defence in depth behind step 4 and `resolve_write_path`'s symlink rejection,
    #    sec.12 / SV5). This closes the TOCTOU window: a link swapped in after the earlier
    #    `lstat` checks makes this open fail with ELOOP — interpreted as a sandbox-escape attempt
    #    and surfaced as the stable `path rejected: outside workspace` message. A regular existing
    #    file is truncated (the approval-gated overwrite).
    working = sandbox.working_copy(Path(entry.root), doc_id)
    working_bytes = working.read_bytes()
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    try:
        fd = os.open(resolved_dest, flags, 0o644)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            # A symlink appeared at the destination after the pre-checks: refuse to follow it.
            _logger.error(
                "save_document_as write refused",
                extra={"detail": f"no-follow open hit a symlink: {exc}"},
            )
            raise ToolError("path rejected: outside workspace") from exc
        _logger.error(
            "save_document_as write failed",
            extra={"detail": f"destination open failed: {exc}"},
        )
        raise ToolError("saved file could not be written") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(working_bytes)
    except OSError as exc:  # pragma: no cover - write failure after a successful open is rare
        raise ToolError("saved file could not be written") from exc
    log_file_io(
        _logger,
        action="save_document_as",
        doc_id=doc_id,
        operation_id=record.operation_id,
        bytes_written=len(working_bytes),
    )

    # 8. POST validation: re-validate the working copy (unchanged by the save) and confirm the
    #    destination was written with a matching byte length and is safe-parseable.
    try:
        post_report = validate_document(doc_id, registry=registry)
    except InspectionError as exc:  # pragma: no cover - working copy was valid pre-save
        raise ToolError("document could not be parsed safely") from exc

    written = resolved_dest.stat().st_size if resolved_dest.is_file() else -1
    if written != len(working_bytes):  # pragma: no cover - copy just succeeded
        raise ToolError("saved file could not be verified after write")

    rel_saved = _relative_to_root(resolved_dest, entry)

    # 9. Link the artifact and transition the record to applied.
    record = update_operation(
        record,
        registry=registry,
        artifacts=[rel_saved],
        status=OperationStatus.APPLIED,
    )

    log_tool_call(
        _logger,
        tool="save_document_as",
        doc_id=doc_id,
        operation_id=record.operation_id,
        overwritten=overwritten,
        risk_class=risk_class.value,
    )
    return SaveResult(
        doc_id=doc_id,
        saved_path=rel_saved,
        operation_id=record.operation_id,
        overwritten=overwritten,
        pre_validation=pre_report,
        post_validation=post_report,
    )
