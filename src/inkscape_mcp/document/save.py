"""Save engine: `save_document_as` + pre/post validation.

Persists the current working-copy state of a registered document to a NEW file inside the
workspace sandbox, running `validate_document` both before and after the write. The
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
raced into the path cannot redirect the `mkdir` outside the sandbox (see
`workspace/paths.py::ensure_parent_within_sandbox`). A `..`-escaping or absolute-outside dest still
creates nothing and is rejected with `path rejected: outside workspace`. The destination is
then validated through the §3 write-path choke point (`resolve_write_path`), which resolves the
parent strictly (following symlinks), containment-checks it, and — when the final component
already exists as a symlink — rejects a link whose real target leaves the sandbox. That resolved
real path is the ONLY value handed to I/O (TOCTOU hardening), and the copy itself refuses to
follow a symlinked destination (`O_NOFOLLOW`) as defence in depth. The tool also refuses,
independently of the `overwrite` flag, to write over any managed document file (an
`original.svg`, a `working/document.svg`, or a registered source path).

Reversibility (ADR-004): an approved overwrite first copies the destination's OLD bytes into the
server-managed `<doc>/backups/<operation_id>.bak` (never into the workspace folder), links it from
the Operation Record (`backup_file`) and returns an `undo` call. `restore_saved_file` (the
`history(action='restore_file')` action) puts those bytes back; it is itself a HIGH-risk,
approval-gated overwrite that backs up the file it replaces, so the undo can be undone.

All client-facing errors are raised as `ToolError` with stable, host-path-free messages.
"""

from __future__ import annotations

import errno
import io
import os
import re
import stat
import sys
from pathlib import Path

from fastmcp.exceptions import ToolError
from pydantic import BaseModel

from inkscape_mcp.config import get_settings
from inkscape_mcp.document.inspect import DocumentNotFound, InspectionError
from inkscape_mcp.logging_setup import get_logger, log_file_io, log_tool_call
from inkscape_mcp.operations import (
    OperationRecord,
    OperationStatus,
    get_operation,
    new_operation,
    update_operation,
)
from inkscape_mcp.registry import DocEntry, Registry, doc_not_found_message, get_registry
from inkscape_mcp.validate import ValidationReport, validate_document
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.paths import (
    HAS_DIR_FD_MKDIR,
    SandboxViolation,
    anchor_to_root,
    ensure_parent_within_sandbox,
    in_state_dir,
    is_contained,
    owning_root,
    resolve_write_path,
)
from inkscape_mcp.workspace.risk import PolicyViolation, RiskClass

_logger = get_logger("document.save")


class SaveResult(BaseModel):
    """Result of `save_document_as`.

    `saved_path` is a WORKSPACE-RELATIVE POSIX path (relative to the document's owning
    workspace root), never an absolute host path, so it can be returned to a client without
    leaking the on-disk layout. `overwritten` is True iff the destination already existed on
    disk before the save (a HIGH-risk, approval-gated overwrite). `pre_validation` and
    `post_validation` are the `validate_document` reports from before and after the write.
    `bytes` is the size of the written file. `undo` (set iff the old destination bytes were backed
    up) is the pasteable `history(... action='restore_file' ...)` call that restores them.
    """

    doc_id: str
    saved_path: str
    operation_id: str
    overwritten: bool
    bytes: int
    pre_validation: ValidationReport
    post_validation: ValidationReport
    undo: str | None = None


class FileRestoreResult(BaseModel):
    """Result of `restore_saved_file`: `undo` is the call that puts the replaced bytes back."""

    doc_id: str
    restored_path: str
    restored_from: str
    operation_id: str
    bytes: int
    undo: str | None = None


class FileRestorePlan(BaseModel):
    """`restore_saved_file(dry_run=True)`: what a restore would write; nothing is written."""

    doc_id: str
    restored_path: str
    operation_id: str
    bytes: int


def _undo_call(doc_id: str, operation_id: str) -> str:
    return f"history(doc_id='{doc_id}', action='restore_file', operation_id='{operation_id}')"


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


_STATE_DIR_MSG = f"cannot write into the managed {sandbox.STATE_DIR} directory"


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


def _resolve_dest(dest_path: str, registry: Registry) -> Path:
    """Sandbox-resolve `dest_path` and refuse protected targets (shared by save and restore)."""
    # Anchor a RELATIVE dest to the workspace root (SV1), NOT the server CWD, then
    # (when the dest names a not-yet-existing subfolder) create the parent tree under a
    # pre-create containment proof (sec.12), and finally resolve through the sandbox write-path
    # choke point (§3). An absolute dest is passed through unchanged and must itself resolve
    # inside a configured root. A containment failure in EITHER step raises SandboxViolation,
    # which is mapped to the safe, host-path-free ToolError below — and the parent-creation
    # step proves containment BEFORE any mkdir, so a rejected dest creates nothing.
    anchored_dest = anchor_to_root(dest_path)
    # Before any mkdir: nothing is created in there.
    if in_state_dir(Path(anchored_dest), get_settings().workspace_roots):
        raise ToolError(_STATE_DIR_MSG)
    try:
        ensure_parent_within_sandbox(anchored_dest)
        resolved_dest = resolve_write_path(anchored_dest)
    except SandboxViolation as exc:
        _logger.error("save_document_as rejected", extra={"detail": exc.detail})
        # Use exc.args[0] explicitly: it is the SAFE public message (no host path). `str(exc)`
        # would happen to resolve to the same value today, but reading args[0] keeps the safe
        # field pinned even if SandboxViolation's str form ever changes.
        raise ToolError(exc.args[0]) from exc

    # Never write into the server's state tree (any document's original / working copy /
    # snapshots / records) nor over a managed document file, whatever `overwrite` says.
    if in_state_dir(_safe_resolve(resolved_dest), get_settings().workspace_roots):
        raise ToolError(_STATE_DIR_MSG)
    if _safe_resolve(resolved_dest) in _managed_paths(registry):
        raise ToolError("cannot overwrite a managed document file")

    # Refuse to write THROUGH a symlink at the destination, full stop. `resolve_write_path`
    # has already rejected a symlink whose real target leaves the sandbox; an IN-sandbox link
    # that reaches here is still refused (we never overwrite via a link, and this gives a clear
    # message instead of the later O_NOFOLLOW ELOOP failure).
    if resolved_dest.is_symlink():
        raise ToolError("cannot overwrite a symbolic link")
    return resolved_dest


_OUTSIDE_MSG = "path rejected: outside workspace"


_ALREADY_EXISTS_MSG = (
    "destination already exists; overwriting requires overwrite=True and an approval_token"
)
_BACKUP_NAME_RE = re.compile(r"op_[0-9a-f]{8}\.bak")
_RESTORABLE_TOOLS = ("save_document_as", "restore_saved_file")
_BIN = getattr(os, "O_BINARY", 0)  # Windows: no newline translation


def _fd_path(fd: int) -> Path | None:
    """Real path an open directory fd points at (Linux `/proc`), or None where unavailable."""
    try:
        return Path(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError:
        return None


def _open_parent(resolved_dest: Path) -> int | None:
    """Open the destination's parent dir `O_DIRECTORY|O_NOFOLLOW` and re-prove its containment.

    The dest is then opened relative to this fd, so a parent swapped to a symlink after
    `_resolve_dest` cannot redirect the write. None on platforms without `dir_fd` (path-based).
    """
    # The explicit platform test lets mypy (--platform win32) treat the fd-relative code below as
    # unreachable there; HAS_DIR_FD_MKDIR covers any other platform lacking dir_fd support.
    if sys.platform == "win32" or not HAS_DIR_FD_MKDIR:
        return None
    try:
        pfd = os.open(resolved_dest.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _BIN)
    except OSError as exc:
        _logger.error("save write refused", extra={"detail": f"parent open failed: {exc}"})
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ToolError(_OUTSIDE_MSG) from exc
        raise ToolError("saved file could not be written") from exc
    real = _fd_path(pfd)
    roots = get_settings().workspace_roots
    if real is None:  # no /proc: containment of the open parent cannot be re-proved
        _logger.warning("save parent containment proof unavailable", extra={"detail": "no /proc"})
    if real is not None and (not is_contained(real, roots) or in_state_dir(real, roots)):
        os.close(pfd)
        _logger.error("save write refused", extra={"detail": "parent fd left the sandbox"})
        raise ToolError(_OUTSIDE_MSG)
    return pfd


def _open_dest(resolved_dest: Path, pfd: int | None, overwrite_ok: bool) -> tuple[int, bool]:
    """Open the destination (no-follow, relative to `pfd`); returns `(fd, created)`.

    Without an approved overwrite the open is `O_CREAT|O_EXCL`, so a file that appeared since the
    MEDIUM-risk decision is refused instead of truncated. With one, an existing file is opened
    in place (never `O_CREAT`), and a missing one is created exclusively.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)  # absent on Windows
    if not nofollow and os.path.islink(resolved_dest):  # no O_NOFOLLOW: explicit last check
        raise ToolError("cannot overwrite a symbolic link")
    target: str | Path = resolved_dest.name if pfd is not None else resolved_dest
    existed = False
    if overwrite_ok:
        try:
            os.stat(target, dir_fd=pfd, follow_symlinks=False)
            existed = True
        except OSError:
            pass
    flags = os.O_RDWR | nofollow | _BIN | getattr(os, "O_NONBLOCK", 0)
    if not existed:
        flags |= os.O_CREAT | os.O_EXCL
    try:
        return os.open(target, flags, 0o644, dir_fd=pfd), not existed
    except FileExistsError as exc:
        if resolved_dest.is_symlink():  # O_EXCL reports a raced-in link as EEXIST, not ELOOP
            raise ToolError(_OUTSIDE_MSG) from exc
        raise ToolError(
            _ALREADY_EXISTS_MSG if not overwrite_ok else "destination changed during save; retry"
        ) from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            # A symlink appeared at the destination after the pre-checks: refuse to follow it.
            _logger.error(
                "save_document_as write refused",
                extra={"detail": f"no-follow open hit a symlink: {exc}"},
            )
            raise ToolError(_OUTSIDE_MSG) from exc
        _logger.error(
            "save_document_as write failed",
            extra={"detail": f"destination open failed: {exc}"},
        )
        raise ToolError("saved file could not be written") from exc


def _check_regular(fd: int, registry: Registry) -> os.stat_result:
    """Refuse a non-regular, hard-linked or managed-file destination, judged on the OPEN fd."""
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise ToolError("cannot overwrite a non-regular file")
    if st.st_nlink > 1:
        raise ToolError("cannot overwrite a hard-linked file")
    for managed in _managed_paths(registry):
        try:
            m = os.stat(managed)
        except OSError:
            continue
        if (m.st_dev, m.st_ino) == (st.st_dev, st.st_ino):
            raise ToolError("cannot overwrite a managed document file")
    return st


def _write_all(handle: io.RawIOBase, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = handle.write(view)
        if not written:
            raise OSError(errno.EIO, "short write")
        view = view[written:]


def _write_with_backup(
    resolved_dest: Path,
    data: bytes,
    root: Path,
    record: OperationRecord,
    registry: Registry,
    overwrite_ok: bool,
) -> str | None:
    """Write `data` over `resolved_dest`, first copying any OLD bytes to the server's backup store.

    The destination is opened ONCE, relative to a no-follow parent dir fd, with `O_NOFOLLOW` (no
    `O_TRUNC`), so the write can NEVER follow a symlink at the final name or in the parent (defence
    in depth behind `_resolve_dest`, sec.12 / SV5): a link swapped in after the pre-checks makes
    the open fail with ELOOP — surfaced as the stable `path rejected: outside workspace`. The open
    fd must be a regular, single-link file that is no managed document file. The old bytes are read
    through that same fd and stored (`O_EXCL|O_NOFOLLOW`, 0600) in
    `<doc>/backups/<operation_id>.bak` BEFORE the file is truncated, and the backup is linked to the
    record at once. A failure after the truncate puts the old bytes back (or removes a file this
    call created) and discards the record. Returns the backup's basename, or None for a new file.
    """
    pfd: int | None = None
    handle: io.FileIO | None = None
    created = False
    old = b""
    backup_file: str | None = None
    truncated = restored = False  # destination was truncated / its old state was put back
    try:
        pfd = _open_parent(resolved_dest)
        fd, created = _open_dest(resolved_dest, pfd, overwrite_ok)
        handle = os.fdopen(fd, "r+b", buffering=0)
        st = _check_regular(fd, registry)
        if not created:
            if st.st_size > get_settings().max_input_bytes:
                raise ToolError(
                    "destination is too large to back up for undo; nothing was overwritten"
                )
            old = handle.readall()
            backup_file = _store_backup(root, record, old)
            record = update_operation(record, registry=registry, backup_file=backup_file)
        try:
            handle.seek(0)
            truncated = True
            handle.truncate(0)
            _write_all(handle, data)
            if os.fstat(fd).st_size != len(data):
                raise OSError(errno.EIO, "short write")
        except BaseException as exc:
            _logger.error("save write failed", extra={"detail": f"write failed: {exc}"})
            try:
                if created:
                    os.unlink(resolved_dest.name if pfd is not None else resolved_dest, dir_fd=pfd)
                else:
                    handle.seek(0)
                    handle.truncate(0)
                    _write_all(handle, old)
                restored = True
            except BaseException as rollback_exc:  # failed: `restored` stays False
                if not isinstance(rollback_exc, Exception):
                    raise
            if not isinstance(exc, Exception):
                raise
            msg = "saved file could not be written"
            if not restored and backup_file:
                msg += (
                    "; the destination may be partial. Put the old bytes back with: "
                    + _undo_call(record.doc_id, record.operation_id)
                )
            raise ToolError(msg) from exc
    except BaseException:
        if truncated and not restored:  # keep the backup reachable: `restore_file` is the way back
            record = update_operation(
                record,
                registry=registry,
                status=OperationStatus.APPLIED,
                logs=[*record.logs, "write failed; destination may be partial: restore the backup"],
            )
        else:
            if backup_file:
                _remove_backup(root, record.doc_id, backup_file)
            update_operation(
                record, registry=registry, backup_file=None, status=OperationStatus.DISCARDED
            )
        raise
    finally:
        if handle is not None:
            handle.close()
        if pfd is not None:
            os.close(pfd)
    return backup_file


def _open_backup(root: Path, doc_id: str, name: str, flags: int, create: bool = False) -> int:
    """Open `<doc>/backups/<name>` with `backups/` proven a REAL directory (never a symlink).

    With `dir_fd` support the dir is opened `O_DIRECTORY|O_NOFOLLOW` relative to the doc dir and the
    file relative to that fd; elsewhere the dir is `lstat`-checked. `create` makes `backups/`.
    """
    docs = sandbox.doc_dir(root, doc_id)
    if sys.platform == "win32" or not HAS_DIR_FD_MKDIR:
        folder = sandbox.backups_dir(root, doc_id)
        if create:
            folder.mkdir(exist_ok=True)
        # lstat reports a Windows junction as a plain directory: refuse it explicitly.
        if folder.is_junction() or not stat.S_ISDIR(os.lstat(folder).st_mode):
            raise OSError(errno.ENOTDIR, "backups is not a real directory")
        return os.open(folder / name, flags, 0o600)
    dfd = os.open(docs, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _BIN)
    try:
        if create:
            try:
                os.mkdir("backups", 0o700, dir_fd=dfd)
            except FileExistsError:
                pass
        bfd = os.open("backups", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _BIN, dir_fd=dfd)
    finally:
        os.close(dfd)
    try:
        return os.open(name, flags, 0o600, dir_fd=bfd)
    finally:
        os.close(bfd)


def _remove_backup(root: Path, doc_id: str, name: str) -> None:
    """Best-effort delete of a backup that is no longer needed."""
    try:
        (sandbox.backups_dir(root, doc_id) / name).unlink(missing_ok=True)
    except OSError:
        pass


def _store_backup(root: Path, record: OperationRecord, old: bytes) -> str:
    """Copy `old` into the server store as `<doc>/backups/<operation_id>.bak` (no-follow, 0600)."""
    name = f"{record.operation_id}.bak"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BIN | getattr(os, "O_NOFOLLOW", 0)
    opened = False
    try:
        fd = _open_backup(root, record.doc_id, name, flags, create=True)
        opened = True
        with os.fdopen(fd, "wb") as out:
            out.write(old)
            out.flush()
            os.fsync(out.fileno())  # durable BEFORE the destination is truncated
    except OSError as exc:
        if opened:
            _remove_backup(root, record.doc_id, name)  # never leave a partial .bak
        _logger.error("backup write failed", extra={"detail": str(exc)})
        raise ToolError("could not back up the destination; nothing was overwritten") from exc
    if hasattr(os, "O_DIRECTORY"):  # best effort: persist the new directory entry too
        try:
            dfd = os.open(sandbox.backups_dir(root, record.doc_id), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    return name


def _read_backup(root: Path, doc_id: str, name: str) -> bytes:
    """Read a stored backup (real regular file, within `max_input_bytes`)."""
    try:
        fd = _open_backup(root, doc_id, name, os.O_RDONLY | _BIN | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            st = os.fstat(handle.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise OSError(errno.EINVAL, "backup is not a regular file")
            if st.st_size > get_settings().max_input_bytes:
                raise ToolError("the backup is too large to restore")
            return handle.read()
    except OSError as exc:
        raise ToolError("the backup is no longer available (pruned or removed)") from exc


def dest_exists(dest_path: str) -> bool:
    """Would `save_document_as(dest_path)` overwrite an existing file? No side-effects (dry_run)."""
    try:
        return resolve_write_path(anchor_to_root(dest_path)).exists()
    except SandboxViolation:
        return False  # missing parent / outside the sandbox: the real call reports it


def save_document_as(
    doc_id: str,
    dest_path: str,
    overwrite: bool = False,
    approval_token: str | None = None,
) -> SaveResult:
    """Save a document's working copy to a NEW workspace file (see the module docstring)."""
    registry = get_registry()

    # 1. PRE validation (also surfaces an unknown / unparseable document early).
    try:
        pre_report = validate_document(doc_id, registry=registry)
    except (DocumentNotFound, KeyError) as exc:
        raise ToolError(doc_not_found_message()) from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc

    entry = registry.get(doc_id)

    resolved_dest = _resolve_dest(dest_path, registry)

    # 5. An already-existing destination is an overwrite → HIGH risk, approval-gated.
    overwritten = resolved_dest.exists()
    if overwritten:
        if not overwrite:
            raise ToolError(_ALREADY_EXISTS_MSG)
        if not (approval_token and approval_token.strip()):
            raise ToolError(
                "destination already exists; overwrite=True also needs an approval_token "
                "(overwriting is HIGH risk)"
            )
        risk_class = RiskClass.HIGH
    else:
        risk_class = RiskClass.MEDIUM

    # The write never alters the working copy, so its post-write validation is computed BEFORE the
    # destination is touched: a validation failure can then never leave a truncated/partial file.
    try:
        post_report = validate_document(doc_id, registry=registry)
    except InspectionError as exc:  # pragma: no cover - working copy was valid pre-save
        raise ToolError("document could not be parsed safely") from exc

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

    # 7. Write the current working-copy bytes to the resolved destination (internal path only),
    #    first backing up any old bytes (ADR-004). See `_write_with_backup` for the no-follow rules.
    working = sandbox.working_copy(Path(entry.root), doc_id)
    working_bytes = working.read_bytes()
    backup_file = _write_with_backup(
        resolved_dest, working_bytes, Path(entry.root), record, registry, overwritten
    )
    log_file_io(
        _logger,
        action="save_document_as",
        doc_id=doc_id,
        operation_id=record.operation_id,
        bytes_written=len(working_bytes),
    )

    rel_saved = _relative_to_root(resolved_dest, entry)

    # 9. Link the artifact and transition the record to applied.
    record = update_operation(
        record,
        registry=registry,
        artifacts=[rel_saved],
        backup_file=backup_file,
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
        bytes=len(working_bytes),
        pre_validation=pre_report,
        post_validation=post_report,
        undo=_undo_call(doc_id, record.operation_id) if backup_file else None,
    )


def restore_saved_file(
    doc_id: str,
    operation_id: str,
    approval_token: str | None = None,
    dry_run: bool = False,
) -> FileRestoreResult | FileRestorePlan:
    """Put back the destination bytes a `save_as` overwrite (or an earlier restore) replaced.

    `operation_id` names the Operation Record that holds the backup. Restoring overwrites the
    file as it stands now, so it is HIGH risk (approval_token required) and goes through the same
    sandbox/symlink/managed-file checks and backup-before-truncate write as `save_document_as`:
    the bytes it replaces are backed up under its OWN record, so the restore can be undone too.
    `dry_run=True` validates and returns a :class:`FileRestorePlan` without a token or any write.
    """
    registry = get_registry()
    try:
        entry = registry.get(doc_id)
    except KeyError as exc:
        raise ToolError(doc_not_found_message()) from exc
    try:
        source = get_operation(doc_id, operation_id, registry=registry)
    except KeyError as exc:
        raise ToolError("operation not found; use the `undo` call from the save result") from exc
    name = source.backup_file
    if (
        not name
        or name != f"{operation_id}.bak"
        or not _BACKUP_NAME_RE.fullmatch(name)
        or "dest_path" not in source.params
        or source.tool not in _RESTORABLE_TOOLS
        # PROPOSED + backup = a crash mid-write left the backup as the only way back
        or source.status not in (OperationStatus.APPLIED, OperationStatus.PROPOSED)
    ):
        raise ToolError("that operation has no overwritten-file backup to restore")
    if not dry_run and not (approval_token and approval_token.strip()):
        raise ToolError(
            "restoring a saved file overwrites it; it needs an approval_token (HIGH risk)"
        )
    root = Path(entry.root)
    data = _read_backup(root, doc_id, name)

    dest_path = str(source.params["dest_path"])
    resolved_dest = _resolve_dest(dest_path, registry)
    if dry_run:
        return FileRestorePlan(
            doc_id=doc_id,
            restored_path=_relative_to_root(resolved_dest, entry),
            operation_id=operation_id,
            bytes=len(data),
        )
    try:
        record = new_operation(
            doc_id,
            tool="restore_saved_file",
            risk_class=RiskClass.HIGH,
            params={"operation_id": operation_id, "dest_path": dest_path},
            registry=registry,
            approval_token=approval_token,
        )
    except PolicyViolation as exc:
        raise ToolError(str(exc)) from exc
    backup_file = _write_with_backup(resolved_dest, data, root, record, registry, True)
    rel = _relative_to_root(resolved_dest, entry)
    record = update_operation(
        record,
        registry=registry,
        artifacts=[rel],
        backup_file=backup_file,
        status=OperationStatus.APPLIED,
    )
    log_tool_call(
        _logger,
        tool="restore_saved_file",
        doc_id=doc_id,
        operation_id=record.operation_id,
        risk_class=RiskClass.HIGH.value,
    )
    return FileRestoreResult(
        doc_id=doc_id,
        restored_path=rel,
        restored_from=operation_id,
        operation_id=record.operation_id,
        bytes=len(data),
        undo=_undo_call(doc_id, record.operation_id) if backup_file else None,
    )
