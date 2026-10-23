"""CLI render/export engine.

Pure functions (no MCP decorators) that build Inkscape CLI argument lists, run them through
`run_inkscape` (`shell=False`, arg lists only, per-process timeout enforced), write outputs
ONLY under the per-document artifact/exports dir, enforce the §4 export limits, and return a
typed result carrying a WORKSPACE-RELATIVE artifact path (never an absolute host path).

Inkscape 1.4.3 flags settled on (empirically verified against /usr/bin/inkscape):

- PNG (preview / document / object):
  ``--export-type=png --export-filename=<out> [--export-width=N] --export-area-page``
  Object: add ``--export-id=<id> --export-id-only`` (and drop ``--export-area-page`` so the
  object's own bbox drives the raster). Verified magic bytes ``\\x89PNG``.
- PDF (document):
  ``--export-filename=<out>.pdf --export-area-page`` — action-pipeline, driven purely by the
  ``.pdf`` filename extension (NO ``--export-type``; PDF is not in the ``--export-type`` list,
  per the capability matrix). Verified to yield a valid ``%PDF-1.5`` file on this host.
- SVG (document):
  ``--export-type=svg --export-plain-svg --export-filename=<out>`` — plain SVG output.

Area choice: whole-document exports use ``--export-area-page`` (the canvas/page box, the
predictable user-facing extent). Single-object exports use ``--export-id-only`` so Inkscape
clips to the object's own bounding box.

Reproducibility / naming (workspace model):

- Previews (change-unlinked whole-doc render) land under ``artifacts/preview/`` named
  ``preview-[<name>-]<descriptor>-<stamp>-<rand>.png``. ``<descriptor>`` encodes the size
  (``<N>px`` when a width is given, else ``auto``); the trailing unique token makes successive
  renders at the SAME width NON-clobbering so an agent can render a before and an after
  without copying out of band. An optional caller ``name`` is folded into the stem.
- User exports land under ``artifacts/exports/`` (or a caller-chosen, sandbox-validated
  ``out_dir``) named ``<utc-timestamp>-<rand>-[<prefix>-]<basename>-<descriptor>.<ext>``
  per §6. The descriptor encodes the reproducible parameters (size/format/object); ``<basename>``
  is a sanitized stem of the document source (the ``doc_id`` for a canvas/svg-text document);
  an optional ``name_prefix`` tags the stem. The
  timestamp + random token make successive exports non-clobbering (and every output is created
  exclusively, so an existing file is never overwritten or later deleted) while the descriptor
  keeps the parameter set reproducible and predictable.

Caller-resolvable location — ``workspace_relative_path`` is always relative to the WORKSPACE ROOT
(a managed output carries the ``.inkscape-mcp/documents/<doc_id>/...`` base; an ``out_dir`` output
is its in-workspace relative path), so a caller opens the file by a single join to the workspace
root with no ``find``/``stat`` for EVERY output. Reported PNG ``width_px``/``height_px`` are the
TRUE on-disk raster dims (read from the written file's IHDR), not a page/viewBox estimate.

SECURITY (sec.12): every argv element is a validated/typed value. Numeric params are coerced
to int and formatted; the input path is the registry's sandbox-validated ``working_path``; the
output path is constructed here under the artifact/exports dir (never from client input). The
object id is validated by the caller (exists in the document + safe charset) before it reaches
``--export-id=``. Dimensions are checked BEFORE invoking Inkscape; output size is checked AFTER
(oversized output is deleted and an error raised). No shell string is ever built.
"""

from __future__ import annotations

import os
import re
import secrets
import struct
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from inkscape_mcp.config import Settings, resolve_settings
from inkscape_mcp.document.inspect import DocSummary, inspect_summary
from inkscape_mcp.engine.ops import (
    ENGINE_EXPORT_FORMATS,
    engine_export_document,
    engine_mode_is_shell,
)
from inkscape_mcp.engine.process import EngineCrash, EngineError
from inkscape_mcp.logging_setup import get_logger, log_export, log_preview
from inkscape_mcp.registry import DocEntry, get_registry
from inkscape_mcp.render.verify import verify_pdf, verify_raster
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.limits import (
    check_export_dimensions,
    check_output_size,
)
from inkscape_mcp.workspace.paths import (
    SandboxViolation,
    ensure_parent_within_sandbox,
    in_state_dir,
    resolve_write_path,
)
from inkscape_mcp.workspace.subprocess_exec import ProcessResult, run_inkscape

_logger = get_logger("render.cli")

#: Supported document export formats (validated by the tool layer too).
PNG = "png"
PDF = "pdf"
SVG = "svg"

#: File extension per format.
_EXT = {PNG: "png", PDF: "pdf", SVG: "svg"}

#: Default raster dimension (px) used to bound the pixel-cap pre-check when a document has no
#: intrinsic/usable size and the caller did not request a width.
_DEFAULT_RASTER_PX = 1024

#: A safe SVG id charset for argv placement. SVG ids follow XML Name rules; we accept a
#: conservative subset (letters, digits, '_', '-', '.', ':') and reject anything else (spaces,
#: slashes, quotes, parens, ...) so no shell/argv-hostile string is ever interpolated.
_SAFE_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]*$")


class RenderError(Exception):
    """Inkscape failed, timed out, or produced no/oversized output.

    The public message is stable and carries no host path.
    """


class InvalidObjectId(Exception):
    """An object id is not present in the document or is not a safe SVG-id token."""


class RenderResult(BaseModel):
    """Outcome of one render/export invocation.

    `workspace_relative_path` is the file relative to the WORKSPACE ROOT, never an absolute host
    path — so a caller opens it by a single join to the workspace root with no `find`/`stat`, for
    both managed and `out_dir` outputs. A managed output carries the
    `.inkscape-mcp/documents/<doc_id>/...` base. `format` is the produced format token;
    `width_px`/`height_px` are the TRUE on-disk raster dimensions for raster (PNG) outputs (read
    from the written file's IHDR) and None for vector PDF/SVG.
    `duration_s` is the Inkscape wall-clock.
    """

    doc_id: str
    workspace_relative_path: str
    format: str
    width_px: int | None
    height_px: int | None
    duration_s: float
    #: STALENESS SIGNAL: True iff the WORKING COPY changed after this artifact was made,
    #: i.e. the artifact no longer reflects the current document and a re-render/export is needed.
    #: Computed at PRODUCE time by `compute_stale` (artifact mtime vs. working-copy mtime): a fresh
    #: artifact is rendered FROM the current working copy and lands with an mtime at/after it, so it
    #: is NOT stale (False). A previously produced artifact whose working copy was edited afterward
    #: reports True. Read-only — no file is ever modified.
    stale: bool = False
    #: CONTENT-TRUTH, computed at PRODUCE time from the just-written artifact:
    #: For a raster (PNG) output, `opaque_px` is the count of drawn (non-transparent) pixels and
    #: `all_blank` is True iff nothing was drawn; both are None for a vector output (no raster).
    #: For a PDF output, `is_vector` is True iff no raster image XObject is embedded and
    #: `fonts_outlined` is True iff no embedded font object is present (true vector when both hold);
    #: both are None for non-PDF outputs. All default None so a hand-built stub (test fakes) and any
    #: artifact whose verification was skipped/failed stay valid.
    opaque_px: int | None = None
    all_blank: bool | None = None
    is_vector: bool | None = None
    fonts_outlined: bool | None = None


class FrameResult(RenderResult):
    """A `RenderResult` for one `capture_frame` call, plus its series + index.

    `series` is the sanitized series folder the frame landed in (under `artifacts/frames/`);
    `frame_index` is the 1-based, monotonically increasing position within that series. Both let a
    script correlate the produced PNGs into an ordered run without re-deriving paths.
    """

    series: str
    frame_index: int


def _mtime(path: Path) -> float | None:
    """Best-effort modification time of `path` in seconds, or None if it cannot be stat'd.

    Read-only — never touches the file. Used to compute the staleness signal by comparing a
    produced artifact's mtime to its source working copy's mtime; a missing/unstattable path yields
    None so the caller treats staleness as "unknown" (False) rather than raising.
    """
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def compute_stale(working_path: Path, artifact_path: Path) -> bool:
    """True iff the working copy is NEWER than the produced artifact.

    Pure + read-only (no file is modified, no new authority): `stat`s both paths and reports the
    artifact as STALE when the working copy's mtime is strictly greater than the artifact's — i.e.
    the document was edited after the artifact was produced, so the artifact no longer reflects the
    current document and a re-render/export is needed. A freshly produced artifact has an mtime at
    or after the working copy's, so it is NOT stale. Returns False ("not stale / unknown") if either
    path cannot be stat'd (e.g. the artifact was deleted), so a stat fault is never an error.
    """
    working_mtime = _mtime(working_path)
    artifact_mtime = _mtime(artifact_path)
    if working_mtime is None or artifact_mtime is None:
        return False
    return working_mtime > artifact_mtime


def is_safe_object_id(object_id: str) -> bool:
    """True iff `object_id` matches the conservative safe SVG-id charset (argv-safe)."""
    return bool(_SAFE_ID_RE.match(object_id))


def _entry(doc_id: str) -> DocEntry:
    """Resolve the registry entry or raise `KeyError` (tool layer maps to ToolError)."""
    return get_registry().get(doc_id)


def _root(entry: DocEntry) -> Path:
    """The owning workspace root as a Path (DocEntry stores it as a str)."""
    return Path(entry.root)


def _intrinsic_size(summary: DocSummary) -> tuple[float, float] | None:
    """Best-effort intrinsic (width, height) in user units from the document summary.

    Prefers the viewBox extent (w, h); falls back to numeric width/height attrs. Returns None
    if neither yields a positive pair.
    """
    if summary.viewbox and len(summary.viewbox) == 4:
        w, h = summary.viewbox[2], summary.viewbox[3]
        if w > 0 and h > 0:
            return w, h

    def _num(value: str | None) -> float | None:
        if not value:
            return None
        match = re.match(r"\s*([0-9]*\.?[0-9]+)", value)
        if not match:
            return None
        try:
            n = float(match.group(1))
        except ValueError:
            return None
        return n if n > 0 else None

    nw = _num(summary.width)
    nh = _num(summary.height)
    if nw is not None and nh is not None:
        return nw, nh
    return None


def _target_raster_dims(summary: DocSummary, width_px: int | None) -> tuple[int, int]:
    """Compute the target raster (width, height) in px for the dimension pre-check.

    Mirrors Inkscape's behaviour: when `--export-width=N` is given, the height scales by the
    document aspect ratio; otherwise the document's intrinsic pixel size is used. The result is
    only used to enforce `check_export_dimensions` BEFORE invoking Inkscape — it is not passed
    as a height arg (Inkscape derives height itself from width + aspect).
    """
    intrinsic = _intrinsic_size(summary)
    if width_px is not None:
        if intrinsic is not None:
            w0, h0 = intrinsic
            height = max(1, round(width_px * (h0 / w0)))
        else:
            height = width_px
        return width_px, height
    if intrinsic is not None:
        w0, h0 = intrinsic
        return max(1, round(w0)), max(1, round(h0))
    return _DEFAULT_RASTER_PX, _DEFAULT_RASTER_PX


def _sanitize_basename(entry: DocEntry) -> str:
    """A filesystem- and name-safe stem: the source file's stem, or the doc_id for a document
    created from a canvas / SVG text (no source file; registered as "document.svg")."""
    # ponytail: "document.svg" sentinel is shared with tools/export.py; a real root-level
    # document.svg is treated as created too (same ambiguity export.py already accepts).
    stem = entry.doc_id if entry.source_path == "document.svg" else Path(entry.source_path).stem
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", stem).strip("-.")
    return cleaned or "document"


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _descriptor(width_px: int | None, *, object_id: str | None = None) -> str:
    """Reproducible descriptor encoding the export parameters (object + size)."""
    parts: list[str] = []
    if object_id is not None:
        parts.append(f"obj-{object_id}")
    parts.append(f"{width_px}px" if width_px is not None else "auto")
    return "-".join(parts)


def _output_name(entry: DocEntry, name_prefix: str | None, descriptor: str, ext: str) -> str:
    """Build a unique export filename ``<stamp>-<rand>-[prefix-]<basename>-<descriptor>.<ext>``.

    The UTC timestamp + random token keep repeat exports (same second, or two identical targets in
    one call) distinct; the sanitized `name_prefix` (when given) lets a caller tag the output. The
    descriptor keeps the parameter set reproducible.
    """
    stamp = f"{_utc_stamp()}-{secrets.token_hex(4)}"
    basename = _sanitize_basename(entry)
    prefix = _safe_name_fragment(name_prefix) if name_prefix is not None else None
    stem = f"{prefix}-{basename}" if prefix else basename
    return f"{stamp}-{stem}-{descriptor}.{ext}"


def _relative_path(entry: DocEntry, out: Path) -> str:
    """Return the workspace-relative path of a produced artifact (POSIX, relative).

    The file relative to the WORKSPACE ROOT (`entry.root`), so an agent opens any artifact by a
    single join to the workspace root with no `find`/`stat`, for EVERY output (managed AND
    caller-chosen `out_dir`). A managed output carries the `.inkscape-mcp/documents/<doc_id>/...`
    base; an `out_dir` output is just its in-workspace relative path.

    `out` is always sandbox-validated, so it is contained by the root; an `out` outside the root is
    a construction bug and raises.
    """
    try:
        return out.relative_to(Path(entry.root)).as_posix()
    except ValueError as exc:  # pragma: no cover - outputs are always validated under the root
        _logger.error("artifact path outside workspace", extra={"doc_id": entry.doc_id})
        raise RenderError("render failed") from exc


def _emit(
    *,
    doc_id: str,
    args: list[str],
    out: Path,
    fmt: str,
    width_px: int | None,
    height_px: int | None,
    entry: DocEntry,
    settings: Settings,
    event: str,
    engine_width_px: int | None = None,
    engine_eligible: bool = False,
) -> RenderResult:
    """Produce the artifact via the warm shell engine when enabled+eligible, else the per-call CLI.

    When `engine_mode == shell` AND `engine_eligible` (a whole-document PNG/SVG export), this first
    tries the warm `inkscape --shell` worker. ANY engine fault transparently falls back to
    the per-call CLI for this op, so the warm path can never regress correctness. `engine_width_px`
    is the requested raster width (None = intrinsic) the engine export should honor.

    `out` is created EXCLUSIVELY and tracked before Inkscape runs, so a pre-existing file is never
    overwritten or deleted, and any failure or cancellation (even a BaseException mid-render)
    removes the half-written output via `discard_on_error`.
    """
    with discard_on_error() as written:
        written.append(_claim_output(out))
        if engine_eligible and engine_mode_is_shell(settings) and fmt in ENGINE_EXPORT_FORMATS:
            start = time.monotonic()
            try:
                engine_export_document(
                    Path(entry.working_path),
                    out,
                    fmt=fmt,
                    width_px=engine_width_px,
                    settings=settings,
                )
                if not _has_output(out):  # the claimed placeholder is still empty
                    raise EngineCrash("warm engine produced no output")
            except EngineError as exc:
                # Never a hard error: the per-call CLI below handles exactly the same op and
                # rewrites the (still claimed, still tracked) output in place.
                _logger.warning(
                    "warm engine export fell back to per-call CLI",
                    extra={"doc_id": doc_id, "format": fmt, "error": type(exc).__name__},
                )
            else:
                return _finalize_output(
                    doc_id=doc_id,
                    out=out,
                    fmt=fmt,
                    width_px=width_px,
                    height_px=height_px,
                    entry=entry,
                    settings=settings,
                    event=event,
                    duration_s=time.monotonic() - start,
                    via="engine",
                )

        return _run_and_finalize(
            doc_id=doc_id,
            args=args,
            out=out,
            fmt=fmt,
            width_px=width_px,
            height_px=height_px,
            entry=entry,
            settings=settings,
            event=event,
        )


def _run_and_finalize(
    *,
    doc_id: str,
    args: list[str],
    out: Path,
    fmt: str,
    width_px: int | None,
    height_px: int | None,
    entry: DocEntry,
    settings: Settings,
    event: str,
) -> RenderResult:
    """Run Inkscape (per-call CLI), enforce success + output-size cap, return the typed result.

    On timeout / non-zero exit / missing output, raises `RenderError`. Only called from `_emit`,
    whose `discard_on_error` block removes the claimed output on any raise.
    """
    result: ProcessResult = run_inkscape(args, settings=settings)
    if result.timed_out:
        _logger.error(
            "render timed out",
            extra={"doc_id": doc_id, "format": fmt, "duration_s": result.duration_s},
        )
        raise RenderError("render timed out")
    if result.returncode != 0 or not _has_output(out):
        _logger.error(
            "render failed",
            extra={"doc_id": doc_id, "format": fmt, "returncode": result.returncode},
        )
        raise RenderError("render failed")

    return _finalize_output(
        doc_id=doc_id,
        out=out,
        fmt=fmt,
        width_px=width_px,
        height_px=height_px,
        entry=entry,
        settings=settings,
        event=event,
        duration_s=result.duration_s,
        via="per_call",
    )


def _finalize_output(
    *,
    doc_id: str,
    out: Path,
    fmt: str,
    width_px: int | None,
    height_px: int | None,
    entry: DocEntry,
    settings: Settings,
    event: str,
    duration_s: float,
    via: str,
) -> RenderResult:
    """Enforce the output-size cap, read true raster dims, build resolvable paths, log + return.

    Shared post-success tail for BOTH the per-call CLI and the warm engine path, so an
    artifact is finalized identically however it was produced. An oversized output raises
    `LimitExceeded` (the caller's `discard_on_error` block deletes it).
    """
    check_output_size(out, settings)

    # Report the TRUE on-disk raster dims for PNG: read the written file's IHDR
    # rather than the page/viewBox-derived target. For vector (PDF/SVG) there is no raster size.
    # If the IHDR read fails for any reason, fall back to the computed target dims.
    opaque_px: int | None = None
    all_blank: bool | None = None
    is_vector: bool | None = None
    fonts_outlined: bool | None = None
    if fmt == PNG:
        actual = _png_dimensions(out)
        if actual is not None:
            width_px, height_px = actual
        # CONTENT-TRUTH: count the drawn pixels so a caller can prove the render is not
        # blank straight from the result. In-process via Pillow (a library, not a subprocess); a
        # decode fault leaves the fields None (unknown) rather than failing the export.
        raster = verify_raster(out)
        if raster is not None:
            opaque_px, all_blank = raster.opaque_px, raster.all_blank
    elif fmt == PDF:
        # CONTENT-TRUTH: certify the PDF is true vector (no raster XObjects, no embedded
        # fonts). In-process byte scan; an unreadable file leaves the fields None (unknown).
        pdf_info = verify_pdf(out)
        if pdf_info is not None:
            is_vector, fonts_outlined = pdf_info.is_vector, pdf_info.fonts_outlined

    ws_rel = _relative_path(entry, out)
    # STALENESS: compute the signal at PRODUCE time by comparing the just-written
    # artifact's mtime to the working copy's. A freshly produced artifact was rendered FROM the
    # current working copy and lands with an mtime at/after it, so it is NOT stale.
    stale = compute_stale(Path(entry.working_path), out)
    log_fn = log_preview if event == "preview" else log_export
    log_fn(
        _logger,
        doc_id=doc_id,
        format=fmt,
        artifact=ws_rel,
        duration_s=duration_s,
        via=via,
    )
    return RenderResult(
        doc_id=doc_id,
        workspace_relative_path=ws_rel,
        format=fmt,
        width_px=width_px,
        height_px=height_px,
        duration_s=duration_s,
        stale=stale,
        opaque_px=opaque_px,
        all_blank=all_blank,
        is_vector=is_vector,
        fonts_outlined=fonts_outlined,
    )


def _has_output(out: Path) -> bool:
    """True iff `out` exists and is non-empty (the claimed placeholder starts empty)."""
    try:
        return out.stat().st_size > 0
    except OSError:
        return False


def _claim_output(out: Path) -> Path:
    """Create `out` EXCLUSIVELY (never reuse an existing file) and return its real path.

    The parent is resolved first and the file is created at the real path with
    `O_CREAT|O_EXCL|O_NOFOLLOW`, so a pre-existing file or planted symlink is refused rather than
    overwritten (and later deleted). The returned real path is what `discard_on_error` tracks.
    """
    try:
        real = out.parent.resolve(strict=True) / out.name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        os.close(os.open(real, flags, 0o644))
    except FileExistsError as exc:
        raise RenderError("export output already exists") from exc
    except OSError as exc:
        raise RenderError("render failed") from exc
    return real


def _safe_unlink(path: Path) -> bool:
    """Delete a tracked real path; True iff something was actually removed.

    `path` was recorded with its RESOLVED parent at claim time; the parent is re-resolved here and
    must still be that same real directory, so a directory swapped for a symlink after the claim can
    never steer the delete elsewhere.
    """
    try:
        if path.parent.resolve(strict=True) != path.parent:
            return False
        path.unlink()
    except OSError:
        return False
    return True


#: Outputs claimed inside the outermost `discard_on_error` block (None = not tracking).
_WRITTEN: ContextVar[list[Path] | None] = ContextVar("render_written", default=None)


@contextmanager
def discard_on_error() -> Iterator[list[Path]]:
    """Track every output claimed in the block; if the block raises, delete them all.

    Makes a multi-file export all-or-nothing: a failure on item N never leaves items 1..N-1 (or a
    half-written item N) behind as a silent half result. Nested blocks share the outermost list, so
    the outermost owner does the cleanup; on a raise its list is narrowed to the paths it really
    deleted. Yields that list.
    """
    outer = _WRITTEN.get()
    if outer is not None:
        yield outer
        return
    written: list[Path] = []
    token = _WRITTEN.set(written)
    try:
        yield written
    except BaseException:
        written[:] = [path for path in written if _safe_unlink(path)]
        raise
    finally:
        _WRITTEN.reset(token)


def _png_size_args(width_px: int | None) -> list[str]:
    """The width arg list for a PNG raster (empty when no width is requested)."""
    return [] if width_px is None else [f"--export-width={int(width_px)}"]


#: 8-byte PNG signature (magic). The IHDR chunk always follows immediately; width and height are
#: the first two big-endian uint32 fields of IHDR, at byte offsets 16..24 of the file.
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_dimensions(path: Path) -> tuple[int, int] | None:
    """Return the TRUE (width, height) in px of a PNG by reading its IHDR (stdlib only).

    Reads only the first 24 bytes: the 8-byte signature, the IHDR length+type, then the two
    big-endian uint32 width/height fields (report the on-disk raster size, not
    a page/viewBox-derived estimate). Returns None if the file is too short, lacks the PNG
    signature, or is not a well-formed IHDR — the caller then falls back to the computed target
    dims rather than misreporting.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(24)
    except OSError:  # pragma: no cover - defensive: output existence already checked
        return None
    if len(head) < 24 or head[:8] != _PNG_SIGNATURE or head[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", head[16:24])
    return int(width), int(height)


def _unique_token() -> str:
    """A short, collision-resistant token for non-clobbering frame names."""
    return f"{_utc_stamp()}-{secrets.token_hex(3)}"


def _safe_name_fragment(value: str) -> str | None:
    """Sanitize a caller-supplied name/prefix into a filesystem- and argv-safe fragment.

    Keeps letters, digits, `_`, `.`, `-`; collapses any other run to a single `-`; trims
    leading/trailing separators. Returns None if nothing usable remains (caller then falls back
    to the generated name). The fragment is only ever used to build an output filename UNDER the
    managed/validated dir — never as a path on its own.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return cleaned or None


def _resolve_out_dir(out_dir: str | None, entry: DocEntry, settings: Settings) -> Path | None:
    """Resolve a caller-chosen `out_dir` to a sandbox-validated directory, or None for default.

    A relative `out_dir` anchors to the WORKSPACE ROOT (`entry.root`), NOT the process CWD.
    Creation is TOCTOU-safe (sec.12): the longest EXISTING prefix is resolved (symlinks followed)
    and `commonpath`-checked against the configured roots BEFORE any side-effect — an escape
    raises `SandboxViolation("path rejected: outside workspace")` with no directory created.
    Missing components are then created relative to a directory file descriptor opened on that
    validated-contained ancestor, descending with `O_NOFOLLOW` so a symlink swapped into the path
    after the check cannot redirect the `mkdir` outside the sandbox. A literal `..` component is
    refused outright. The result is re-validated through `resolve_write_path` (the single sandbox
    choke point with the final-component symlink guard). Returns the validated real directory, or
    None when `out_dir` is omitted (managed-dir back-compat).
    """
    if out_dir is None:
        return None
    candidate = Path(out_dir)
    if not candidate.is_absolute():
        candidate = Path(entry.root) / candidate
    # The probe final-component makes `candidate` the parent that gets contained + created
    # (TOCTOU-safe, `..`-refusing) by the shared helper, then re-validated by the choke point
    # (parent normalization + containment + final-component symlink guard).
    state_dir_msg = f"cannot write into the managed {sandbox.STATE_DIR} directory"
    if in_state_dir(candidate, settings.workspace_roots):  # lexical, before any mkdir
        raise SandboxViolation(state_dir_msg, detail=f"out_dir {out_dir!r} is in the state dir")
    probe = candidate / ".probe"
    ensure_parent_within_sandbox(probe, settings)
    resolved = resolve_write_path(probe, settings).parent
    if in_state_dir(resolved, settings.workspace_roots):  # an existing dir or symlink into it
        raise SandboxViolation(state_dir_msg, detail=f"out_dir {out_dir!r} resolves into state dir")
    return resolved


# --- Public engine functions ------------------------------------------------


def render_preview(
    doc_id: str,
    width_px: int | None = None,
    name: str | None = None,
    settings: Settings | None = None,
) -> RenderResult:
    """Render a PNG preview of the whole document into `artifacts/preview/`.

    Enforces the pixel cap before invoking Inkscape. Successive calls at the same width do NOT
    clobber: when `name` is given it controls the frame stem (`preview-<name>-...`),
    otherwise a unique token is minted per call so a before/after pair at one width yields two
    distinct files. Returns a resolvable artifact path.
    """
    s = resolve_settings(settings)
    entry = _entry(doc_id)
    summary = inspect_summary(doc_id)

    target_w, target_h = _target_raster_dims(summary, width_px)
    check_export_dimensions(target_w, target_h, s)

    root = _root(entry)
    sandbox.ensure_doc_dirs(root, doc_id)
    preview_dir = sandbox.artifacts_dir(root, doc_id) / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    # Unique per call so re-renders at the same width never overwrite. An optional
    # caller `name` is sanitized and folded into the stem; the unique token still guarantees
    # distinctness even if the same name is reused.
    parts = ["preview"]
    if name is not None:
        fragment = _safe_name_fragment(name)
        if fragment is not None:
            parts.append(fragment)
    parts.append(_descriptor(width_px))
    parts.append(_unique_token())
    out = preview_dir / f"{'-'.join(parts)}.png"

    args = [
        str(entry.working_path),
        "--export-type=png",
        f"--export-filename={out}",
        *_png_size_args(width_px),
        "--export-area-page",
    ]
    return _emit(
        doc_id=doc_id,
        args=args,
        out=out,
        fmt=PNG,
        width_px=target_w,
        height_px=target_h,
        entry=entry,
        settings=s,
        event="preview",
        engine_width_px=width_px,
        engine_eligible=True,
    )


#: Matches a generated frame stem so the next series index can be derived from the filesystem
#: (``frame-007.png`` / ``frame-007-label.png`` → 7). Anchored so only our own frames count.
_FRAME_NAME_RE = re.compile(r"^frame-(\d+)(?:-.*)?\.png$")


def _next_frame_index(series_dir: Path) -> int:
    """Next 1-based frame index for `series_dir`, derived from the existing ``frame-NNN`` files.

    Stateless (survives a server restart) and matches the non-clobber philosophy: the
    counter is the max existing index + 1, never in-memory state. Returns 1 for an empty/new series.
    """
    highest = 0
    for child in series_dir.iterdir():
        # Ignore symlinks: a planted ``frame-NNN.png`` link must not steer the series numbering.
        if child.is_symlink():
            continue
        match = _FRAME_NAME_RE.match(child.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def capture_frame(
    doc_id: str,
    series: str | None = None,
    width_px: int | None = None,
    label: str | None = None,
    settings: Settings | None = None,
) -> FrameResult:
    """Render the whole document to the next numbered PNG frame in a per-run series.

    A scripted edit sequence calls this at each step to build an ordered ``frame-001.png``,
    ``frame-002.png``, … screenshot series under ``artifacts/frames/<series>/``. The frame index is
    derived from the filesystem (max existing ``frame-NNN`` + 1), so it is monotonic, restart-proof,
    and never clobbers an existing frame (the index is bumped past any name collision). `series`
    (sanitized; defaults to ``run``) groups one run's frames into their own folder; an optional
    `label` is folded into the stem. Same whole-doc PNG pipeline, caps, and warm-engine path as
    `render_preview`. Returns a resolvable artifact path plus the `series` + `frame_index`.
    """
    s = resolve_settings(settings)
    # Guard a non-positive width before it can reach ``--export-width=`` (the dimension cap only
    # bounds the upper end); a negative/zero raster is meaningless.
    if width_px is not None and width_px < 1:
        raise RenderError("width_px must be a positive integer")
    entry = _entry(doc_id)
    summary = inspect_summary(doc_id)

    target_w, target_h = _target_raster_dims(summary, width_px)
    check_export_dimensions(target_w, target_h, s)

    root = _root(entry)
    sandbox.ensure_doc_dirs(root, doc_id)
    # `series`/`label` are sanitized to a single argv-/filesystem-safe fragment (slashes, `..`, and
    # argv-hostile chars are collapsed), so `series` can only ever name a sub-dir UNDER the managed
    # frames dir — never a path that escapes the per-doc artifacts tree.
    safe_series = (_safe_name_fragment(series) if series is not None else None) or "run"
    frames_dir = sandbox.artifacts_dir(root, doc_id) / "frames" / safe_series
    # A filesystem fault here (e.g. PermissionError) carries an absolute host path — map it to the
    # stable, host-path-free RenderError so the path never reaches the client (sec.12).
    try:
        frames_dir.mkdir(parents=True, exist_ok=True)
        idx = _next_frame_index(frames_dir)
    except OSError as exc:
        raise RenderError("render failed") from exc

    label_fragment = _safe_name_fragment(label) if label is not None else None
    while True:
        stem = f"frame-{idx:03d}" + (f"-{label_fragment}" if label_fragment else "")
        out = frames_dir / f"{stem}.png"
        if not out.exists():
            break
        # A label-carrying frame can collide with a plain one at the same index; bump past it so a
        # capture never overwrites an earlier frame (non-clobber).
        idx += 1

    args = [
        str(entry.working_path),
        "--export-type=png",
        f"--export-filename={out}",
        *_png_size_args(width_px),
        "--export-area-page",
    ]
    base = _emit(
        doc_id=doc_id,
        args=args,
        out=out,
        fmt=PNG,
        width_px=target_w,
        height_px=target_h,
        entry=entry,
        settings=s,
        event="preview",
        engine_width_px=width_px,
        engine_eligible=True,
    )
    return FrameResult(**base.model_dump(), series=safe_series, frame_index=idx)


class FrameRef(BaseModel):
    """One existing frame in a series: its index, on-disk name, and resolvable workspace path."""

    frame_index: int
    name: str
    workspace_relative_path: str


class FrameListing(BaseModel):
    """A `capture_frame` series + its frames, ordered by index (resolved series name + refs)."""

    series: str
    frames: list[FrameRef]


def list_frames(
    doc_id: str, series: str | None = None, settings: Settings | None = None
) -> FrameListing:
    """List the frames in a `capture_frame` series, ordered by index (read-only).

    Lets a script gather a whole run's PNGs at the end without re-deriving paths. `series` is
    sanitized identically to `capture_frame` (defaults to ``run``); the resolved name is returned on
    the result. ``frames`` is empty when the series folder does not exist yet. Only files matching
    the generated ``frame-NNN`` stem are reported.
    """
    entry = _entry(doc_id)
    safe_series = (_safe_name_fragment(series) if series is not None else None) or "run"
    frames_dir = sandbox.artifacts_dir(_root(entry), doc_id) / "frames" / safe_series
    # Best-effort read: a missing dir or a filesystem fault yields an empty listing rather than
    # surfacing an OSError (whose message would carry an absolute host path — sec.12).
    try:
        children = list(frames_dir.iterdir()) if frames_dir.is_dir() else []
    except OSError:
        children = []
    refs: list[FrameRef] = []
    for child in children:
        # Skip symlinks: a planted link matching the frame stem must never be returned as a
        # resolvable workspace path (following it could escape the sandbox — sec.12).
        if child.is_symlink():
            continue
        match = _FRAME_NAME_RE.match(child.name)
        if not match:
            continue
        ws_rel = _relative_path(entry, child)
        refs.append(
            FrameRef(
                frame_index=int(match.group(1)),
                name=child.name,
                workspace_relative_path=ws_rel,
            )
        )
    refs.sort(key=lambda ref: (ref.frame_index, ref.name))
    return FrameListing(series=safe_series, frames=refs)


def export_document(
    doc_id: str,
    fmt: str,
    width_px: int | None = None,
    out_dir: str | None = None,
    name_prefix: str | None = None,
    *,
    _extra_args: list[str] | None = None,
    settings: Settings | None = None,
) -> RenderResult:
    """Export the whole document to PNG / PDF / SVG.

    `fmt` must be one of {png, pdf, svg} (validated). PNG honors `width_px` and is pixel-capped
    before invocation; PDF/SVG are vector and ignore `width_px`. By default the artifact lands in
    the managed per-doc `artifacts/exports/` dir; an optional `out_dir` (relative paths anchored to
    the workspace ROOT, then sandbox-validated) targets a caller-chosen directory and an
    optional `name_prefix` tags the filename stem. `_extra_args` are extra Inkscape flags appended
    verbatim — keyword-only, server-internal only (the print profile passes its print flags here),
    NEVER client-sourced: each is hard-validated to be an `--export-` flag before reaching argv.
    Returns a resolvable artifact path.
    """
    s = resolve_settings(settings)
    if fmt not in _EXT:
        raise RenderError(f"unsupported export format: {fmt!r}")
    # Defense in depth (sec.12): even though `_extra_args` is server-internal, refuse anything that
    # is not an `--export-*` flag, and explicitly refuse `--export-filename` so a future caller can
    # never smuggle a non-export arg OR an output-path override (which would bypass the sandbox)
    # into argv. The output path is constructed below, never from `_extra_args`.
    if _extra_args and any(
        not a.startswith("--export-") or a.startswith("--export-filename") for a in _extra_args
    ):
        raise RenderError("internal export flags must be --export-* options (no --export-filename)")
    entry = _entry(doc_id)

    target_w: int | None = None
    target_h: int | None = None
    if fmt == PNG:
        summary = inspect_summary(doc_id)
        target_w, target_h = _target_raster_dims(summary, width_px)
        check_export_dimensions(target_w, target_h, s)

    root = _root(entry)
    sandbox.ensure_doc_dirs(root, doc_id)
    dest = _resolve_out_dir(out_dir, entry, s)
    exports = dest if dest is not None else sandbox.exports_dir(root, doc_id)
    descriptor = _descriptor(width_px if fmt == PNG else None)
    name = _output_name(entry, name_prefix, descriptor, _EXT[fmt])
    out = exports / name
    extra = list(_extra_args) if _extra_args else []

    if fmt == PNG:
        args = [
            str(entry.working_path),
            "--export-type=png",
            f"--export-filename={out}",
            *_png_size_args(width_px),
            "--export-area-page",
            *extra,
        ]
    elif fmt == PDF:
        # PDF is action-pipeline: driven by the .pdf filename extension, NO --export-type
        # (PDF is not in the --export-type list per the capability matrix).
        args = [
            str(entry.working_path),
            f"--export-filename={out}",
            "--export-area-page",
            *extra,
        ]
    else:  # SVG (plain)
        args = [
            str(entry.working_path),
            "--export-type=svg",
            "--export-plain-svg",
            f"--export-filename={out}",
            *extra,
        ]

    # Whole-doc PNG/SVG are warm-engine-eligible; PDF and any server-internal `_extra_args`
    # (the print profile's flags, which the shell export line does not replicate) stay per-call.
    engine_eligible = fmt in ENGINE_EXPORT_FORMATS and not extra
    return _emit(
        doc_id=doc_id,
        args=args,
        out=out,
        fmt=fmt,
        width_px=target_w,
        height_px=target_h,
        entry=entry,
        settings=s,
        event="export",
        engine_width_px=width_px if fmt == PNG else None,
        engine_eligible=engine_eligible,
    )


def export_object(
    doc_id: str,
    object_id: str,
    fmt: str = PNG,
    width_px: int | None = None,
    out_dir: str | None = None,
    name_prefix: str | None = None,
    settings: Settings | None = None,
) -> RenderResult:
    """Export a single object (by id) to PNG / PDF / SVG.

    The caller MUST validate that `object_id` exists in the document; this function additionally
    enforces the safe-id charset and refuses any id that fails it, so no argv-hostile string is
    ever placed into `--export-id=`. Uses `--export-id-only` so Inkscape clips to the object's
    own bounding box. By default the artifact lands in the managed per-doc `artifacts/exports/`
    dir; an optional `out_dir` (relative paths anchored to the workspace ROOT, then
    sandbox-validated) targets a caller-chosen directory and an optional `name_prefix`
    tags the filename stem. Returns a resolvable artifact path.
    """
    s = resolve_settings(settings)
    if fmt not in _EXT:
        raise RenderError(f"unsupported export format: {fmt!r}")
    if not is_safe_object_id(object_id):
        raise InvalidObjectId("object id is not a safe svg id")
    entry = _entry(doc_id)

    target_w: int | None = None
    target_h: int | None = None
    if fmt == PNG:
        summary = inspect_summary(doc_id)
        # Object bbox size is unknown without rendering; cap against the page extent as a safe
        # upper bound (the clipped object can never exceed the page raster at the same width).
        target_w, target_h = _target_raster_dims(summary, width_px)
        check_export_dimensions(target_w, target_h, s)

    root = _root(entry)
    sandbox.ensure_doc_dirs(root, doc_id)
    dest = _resolve_out_dir(out_dir, entry, s)
    exports = dest if dest is not None else sandbox.exports_dir(root, doc_id)
    descriptor = _descriptor(width_px if fmt == PNG else None, object_id=object_id)
    name = _output_name(entry, name_prefix, descriptor, _EXT[fmt])
    out = exports / name

    base = [
        str(entry.working_path),
        f"--export-id={object_id}",
        "--export-id-only",
    ]
    if fmt == PNG:
        args = [*base, "--export-type=png", f"--export-filename={out}", *_png_size_args(width_px)]
    elif fmt == PDF:
        args = [*base, f"--export-filename={out}"]
    else:  # SVG (plain)
        args = [*base, "--export-type=svg", "--export-plain-svg", f"--export-filename={out}"]

    return _emit(
        doc_id=doc_id,
        args=args,
        out=out,
        fmt=fmt,
        width_px=target_w,
        height_px=target_h,
        entry=entry,
        settings=s,
        event="export",
    )
