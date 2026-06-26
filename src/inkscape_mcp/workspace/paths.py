"""Path safety (workspace model) — the single sandbox-validation choke point.

Every path entering the server passes through `resolve_read_path` / `resolve_write_path`
before any filesystem access. The rules are applied in order: reject empty/NUL, resolve to
a canonical real path (following all symlinks), then a `commonpath`-based containment check
against the configured roots. On the write path the not-yet-existing final component is
exempt from existence, but if it ALREADY exists as a symlink its real target is re-checked
for containment (an out-of-sandbox link is rejected, sec.12). The RESOLVED REAL PATH is the
only value that should ever be handed to `open()` / `copy()` / a subprocess argv (TOCTOU
hardening, §3).

Sandbox-violation public messages NEVER include a host filesystem path (sec.12 /
fastmcp-patterns error model); the offending raw path is kept only in the private detail.
"""

from __future__ import annotations

import os
from pathlib import Path

from inkscape_mcp.config import Settings, get_settings


class SandboxViolation(Exception):
    """A path failed sandbox validation.

    `args[0]` is a SAFE public message containing no host filesystem path. `detail` carries
    the private explanation (may include the raw path) for server-side logging only.
    """

    def __init__(self, message: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail


def is_contained(p: Path, roots: list[Path]) -> bool:
    """True iff resolved path `p` is `R` or lives under `R` for some resolved root `R`.

    Containment is tested with `os.path.commonpath` on canonical real paths (§3): `p` is
    allowed iff `commonpath([R, p]) == R`. Assumes both `p` and the roots are already
    resolved real paths.
    """
    for root in roots:
        try:
            if os.path.commonpath([str(root), str(p)]) == str(root):
                return True
        except ValueError:
            # Different drives / mixed absolute-relative: not contained by this root.
            continue
    return False


def owning_root(p: Path, roots: list[Path]) -> Path | None:
    """Return the single resolved root that contains `p`, or None if none does."""
    for root in roots:
        try:
            if os.path.commonpath([str(root), str(p)]) == str(root):
                return root
        except ValueError:
            continue
    return None


def anchor_to_root(raw: str | Path, settings: Settings | None = None) -> str:
    """Anchor a RELATIVE path to the FIRST configured workspace root (the shared anchor contract).

    A relative path (the common, friendly form) is joined to `workspace_roots[0]` — the document
    root — so it never resolves relative to the server's process CWD (SV1 /). An ABSOLUTE path is returned unchanged; the sandbox choke point
    (`resolve_read_path` / `resolve_write_path`) then enforces that it still lands inside a
    configured root. When no workspace root is configured the raw value is returned and that same
    choke point rejects it cleanly. This helper does NO containment work itself — it only chooses
    the anchor; the `..`/symlink/escape guard stays the sole authority of the resolve choke point,
    so the escape surface is not widened (a relative `../` join still resolves outside the root and
    is rejected with `path rejected: outside workspace`).
    """
    candidate = Path(os.fspath(raw))
    if candidate.is_absolute():
        return os.fspath(raw)
    s = settings if settings is not None else get_settings()
    if not s.workspace_roots:
        return os.fspath(raw)
    return str(s.workspace_roots[0] / candidate)


def ensure_parent_within_sandbox(anchored: str | Path, settings: Settings | None = None) -> None:
    """Create a write destination's missing parent dirs INSIDE the sandbox — TOCTOU-safe (sec.12).

    `anchored` is a destination already anchored to the workspace root (relative dests resolved via
    `anchor_to_root`, absolute dests untouched). When the parent already exists this is a no-op;
    when one or more leading components are missing they are created so a caller can write into a
    fresh subfolder without a manual `mkdir -p` first (mirrors `save_document_as`). The
    creation can NEVER escape the sandbox, proven BEFORE any side-effect (mirrors the audited
    `render/cli.py::_resolve_out_dir`/`tools/save.py::_ensure_parent_dir` pattern):

    1. A literal `..` anywhere in the destination is refused outright (`path rejected: outside
       workspace`) — nothing is created.
    2. The longest EXISTING ancestor of the parent is resolved with `strict=True` (every symlink
       followed) and `commonpath`-checked against the configured roots. An ancestor resolving
       outside every root is rejected before anything is created.
    3. The missing tail is created one component at a time relative to a directory file descriptor
       opened on that validated-contained ancestor, descending with
       `O_RDONLY|O_DIRECTORY|O_NOFOLLOW`; if any component is (or is raced into) a symlink the
       descend `open` fails with `ELOOP`/`ENOTDIR` and creation aborts — the side-effect can never
       escape the validated ancestor.

    No host path appears in any raised message. The final-component symlink guard and the
    no-follow write at each call site stay the authority on the destination FILE itself; this helper
    only ever creates the PARENT tree. Raises `SandboxViolation` on any escape attempt.
    """
    s = _settings_or_default(settings)
    anchored_str = os.fspath(anchored)
    parent = Path(anchored_str).parent
    if any(part == ".." for part in Path(anchored_str).parts):
        raise SandboxViolation(
            "path rejected: outside workspace",
            detail=f"dest {anchored_str!r} contains a '..' component",
        )

    # Longest existing ancestor (PRE-CREATE CONTAINMENT): resolve + commonpath-check it BEFORE
    # creating anything (sec.12), so an escape never reaches the mkdir side-effect. A bare OSError
    # from the existence probe (e.g. EACCES on an intermediate component) is folded into a
    # host-path-free SandboxViolation rather than propagating a raw error that could carry a path.
    existing = parent
    try:
        while not existing.exists():
            nxt = existing.parent
            if nxt == existing:  # reached the filesystem root
                break
            existing = nxt
    except OSError as exc:
        raise SandboxViolation(
            "path rejected: could not resolve path",
            detail=f"dest parent existence probe failed for {anchored_str!r}: {exc}",
        ) from None
    try:
        resolved_existing = existing.resolve(strict=True)
    except OSError as exc:
        raise SandboxViolation(
            "path rejected: could not resolve path",
            detail=f"dest parent prefix resolve failed: {exc}",
        ) from None
    if not is_contained(resolved_existing, s.workspace_roots):
        raise SandboxViolation(
            "path rejected: outside workspace",
            detail=f"dest {anchored_str!r} parent resolves outside all configured roots",
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
    escape the base dir. Mirrors `render/cli.py::_safe_mkdir_chain`/`tools/save.py`.
    """
    dir_fd = os.open(base_real_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in components:
            if part in ("", ".", ".."):
                raise SandboxViolation(
                    "path rejected: invalid filename",
                    detail=f"unsafe dest component {part!r}",
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
                    detail=f"dest component {part!r} is not a real directory: {exc}",
                ) from None
            # Reassign BEFORE closing so `finally` always closes the live fd even if close() raises.
            old_fd, dir_fd = dir_fd, next_fd
            os.close(old_fd)
    finally:
        os.close(dir_fd)


def _reject_empty_or_nul(raw: str | Path) -> str:
    """Reject empty paths and paths containing a NUL byte (§3 normalization step 1)."""
    s = os.fspath(raw)
    if s == "" or s.strip() == "":
        raise SandboxViolation("path rejected: empty path", detail="empty path")
    if "\x00" in s:
        raise SandboxViolation("path rejected: invalid characters", detail="NUL byte in path")
    return s


def _settings_or_default(settings: Settings | None) -> Settings:
    s = settings if settings is not None else get_settings()
    if not s.workspace_roots:
        raise SandboxViolation(
            "path rejected: no workspace root configured",
            detail="no usable workspace root in settings",
        )
    return s


def resolve_read_path(raw: str | Path, settings: Settings | None = None) -> Path:
    """Validate and canonicalize a path whose target MUST already exist (§3 read path).

    Rejects empty/NUL, resolves with `strict=True` (every symlink followed), then enforces
    containment. Returns the canonical real `Path`. Raises `SandboxViolation` otherwise.
    The returned path is the only value safe to hand to I/O.
    """
    s = _settings_or_default(settings)
    raw_str = _reject_empty_or_nul(raw)
    try:
        resolved = Path(raw_str).resolve(strict=True)
    except FileNotFoundError:
        raise SandboxViolation(
            "path rejected: file not found",
            detail=f"read target not found: {raw_str!r}",
        ) from None
    except OSError as exc:
        raise SandboxViolation(
            "path rejected: could not resolve path",
            detail=f"resolve failed for {raw_str!r}: {exc}",
        ) from None
    if not is_contained(resolved, s.workspace_roots):
        raise SandboxViolation(
            "path rejected: outside workspace",
            detail=f"resolved path {str(resolved)!r} is outside all configured roots",
        )
    return resolved


def resolve_write_path(raw: str | Path, settings: Settings | None = None) -> Path:
    """Validate a path whose final component may not exist yet (§3 write/create path).

    Resolves the PARENT directory with `strict=True` (raising if any intermediate is
    missing — so a symlinked intermediate added later cannot be partially trusted),
    containment-checks the resolved parent, then appends the single final filename
    component. Only the final name is exempt from existence. If the final component already
    exists on disk, it is `lstat`-checked and REJECTED when it is a symlink whose real
    target leaves the sandbox — closing the symlink-at-the-destination-filename escape that
    `shutil.copyfile`/`open()` would otherwise follow on overwrite (sec.12). Returns the
    constructed real path. Raises `SandboxViolation` otherwise.

    CALLER CONTRACT (TOCTOU): the symlink check and the eventual write are not atomic — a link
    planted at the final name between this call and the write could still be followed by a naive
    `open()`. The returned path MUST therefore be opened/created with `O_NOFOLLOW` (or an
    equivalent no-follow primitive) so a same-name symlink swapped in after this check cannot be
    followed. `tools/save.py` does exactly this; any new writer through this choke point must too.
    """
    s = _settings_or_default(settings)
    raw_str = _reject_empty_or_nul(raw)

    candidate = Path(raw_str)
    final_name = candidate.name
    if final_name in ("", ".", ".."):
        raise SandboxViolation(
            "path rejected: invalid filename",
            detail=f"no usable final filename component in {raw_str!r}",
        )

    parent = candidate.parent
    try:
        resolved_parent = parent.resolve(strict=True)
    except FileNotFoundError:
        raise SandboxViolation(
            "path rejected: parent directory not found",
            detail=f"write parent not found for {raw_str!r}",
        ) from None
    except OSError as exc:
        raise SandboxViolation(
            "path rejected: could not resolve parent",
            detail=f"parent resolve failed for {raw_str!r}: {exc}",
        ) from None

    if not resolved_parent.is_dir():
        raise SandboxViolation(
            "path rejected: parent is not a directory",
            detail=f"write parent is not a directory for {raw_str!r}",
        )

    if not is_contained(resolved_parent, s.workspace_roots):
        raise SandboxViolation(
            "path rejected: outside workspace",
            detail=f"resolved parent {str(resolved_parent)!r} is outside all configured roots",
        )

    final_path = resolved_parent / final_name

    # FINAL-COMPONENT GUARD (sec.12 / SV5): the parent is contained, but the final name may
    # ALREADY be a pre-existing symlink pointing outside the sandbox. `lstat` the final
    # component WITHOUT following it; if it is a symlink, resolve its real target and re-run
    # the containment check. A target that leaves the sandbox is rejected — nothing is ever
    # written through such a link, even on overwrite. (TOCTOU note: a link planted between
    # this check and the write still cannot escape, because callers write to `final_path`,
    # whose parent is canonical; defence-in-depth lives in `tools/save.py`.)
    try:
        is_link = final_path.is_symlink()
    except OSError as exc:
        raise SandboxViolation(
            "path rejected: could not resolve path",
            detail=f"lstat failed for {raw_str!r}: {exc}",
        ) from None
    if is_link:
        # strict=False: the write destination need not exist, and a DANGLING link (target
        # absent) must still be containment-checked. `resolve` normalizes the target to an
        # absolute path; `is_contained` then tests that normalized form via `commonpath`, so a
        # multi-hop or `..`-laden link that lands outside any root is rejected.
        try:
            link_real = final_path.resolve(strict=False)
        except OSError as exc:
            raise SandboxViolation(
                "path rejected: could not resolve path",
                detail=f"symlink resolve failed for {raw_str!r}: {exc}",
            ) from None
        if not is_contained(link_real, s.workspace_roots):
            raise SandboxViolation(
                "path rejected: outside workspace",
                detail=(
                    f"final component {str(final_path)!r} is a symlink to "
                    f"{str(link_real)!r}, outside all configured roots"
                ),
            )

    return final_path
