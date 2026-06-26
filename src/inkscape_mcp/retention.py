"""Snapshot + live-frame retention / cleanup pass (follow-up).

Pure functions implementing the deterministic snapshot retention policy from workspace-model §6:
retain the UNION of the last `keep_n` snapshots and every snapshot within `keep_days`, bounded
above by the absolute hard caps (count + bytes); prune everything else. `original.svg` (the
seq-0000 baseline) and the live head `working/document.svg` are NEVER pruned — neither is a
snapshot in `index.json`, so the policy can only ever remove superseded intermediate snapshots
and the reversibility chain back to `original` stays intact. Orphaned Operation Records — those
whose linked pre-mutation snapshot was pruned — are removed with their snapshots so every
surviving snapshot keeps an explaining record.

folds LIVE-FRAME retention into this SAME explicit sweep: rasterized live frames accumulate
under the root-scoped live artifacts dir and are pruned by age (`live_frame_keep_days`) and a total
byte budget (`live_frame_max_bytes`, newest kept). This is deliberately part of the explicit pass —
NEVER a side effect of a live mutating tool — per project policy.

Cleanup is an EXPLICIT pass only — the boot-time `sweep_all_roots()` and the `prune_snapshots`
maintenance tool — never an implicit side effect of a mutating tool, so a single edit can never
silently destroy restore history (§6) and a live render never quietly deletes prior frames.

Path safety (sec.12): snapshot files are addressed by their manifest BASENAME under the
sandbox-derived `snapshots/` dir; a basename guard refuses to delete any tampered index entry
that carries a path component, so a crafted manifest cannot redirect an unlink out of the dir.
Live-frame pruning enumerates real files directly under the sandbox-derived live artifacts dir and
unlinks only regular files there. The sweep enumerates real on-disk document directories under each
configured root and operation records via `op_*.json` globs; it never trusts a client-supplied path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field

from inkscape_mcp.config import Settings, get_settings
from inkscape_mcp.live.records import LiveOperationRecord
from inkscape_mcp.logging_setup import get_logger, log_file_io
from inkscape_mcp.operations import OperationRecord
from inkscape_mcp.registry import Registry, get_registry
from inkscape_mcp.snapshots import SnapshotInfo, _read_index, _write_index
from inkscape_mcp.workspace import sandbox

_logger = get_logger("retention")


class LivePruneResult(BaseModel):
    """Outcome of pruning loop/live frames under one root's live artifacts dir."""

    pruned_frames: int = 0
    freed_bytes: int = 0
    retained_frames: int = 0


class PruneResult(BaseModel):
    """Outcome of pruning one document's snapshots under the §6 retention policy.

    `live_frames` carries the loop-frame prune for the document's OWN root (the maintenance
    tool prunes live frames in the same explicit pass; null when invoked path-only). It is purely
    additive — it never affects which snapshots or records are retained."""

    doc_id: str
    pruned_snapshot_ids: list[str] = Field(default_factory=list)
    pruned_operation_ids: list[str] = Field(default_factory=list)
    retained_count: int
    freed_bytes: int
    live_frames: LivePruneResult | None = None


class SweepResult(BaseModel):
    """Outcome of a full retention sweep across every document under every configured root."""

    documents: list[PruneResult] = Field(default_factory=list)
    pruned_snapshots: int
    pruned_operations: int
    freed_bytes: int
    live_frames: list[LivePruneResult] = Field(default_factory=list)
    pruned_live_frames: int = 0
    freed_live_bytes: int = 0


def _within_keep_days(created_at: str, now: datetime, keep_days: int) -> bool:
    """True if `created_at` (ISO-8601) falls within the last `keep_days`.

    An unparseable timestamp is treated as IN-window (retained) — the policy never prunes a
    snapshot on a malformed clock value, so a bad index entry cannot cause data loss.
    """
    try:
        ts = datetime.fromisoformat(created_at)
    except ValueError:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts >= now - timedelta(days=keep_days)


def _select_retained(entries: list[SnapshotInfo], settings: Settings, now: datetime) -> set[str]:
    """Compute the set of snapshot ids to RETAIN under the §6 policy.

    Base keep = union of (the most recent `keep_n` by seq) and (every snapshot within
    `keep_days`). The absolute hard caps then bound that set from above: at most
    `snapshot_hard_max_n` entries and at most `snapshot_hard_max_bytes` of data are retained,
    dropping the OLDEST first even if it sits inside the keep window. Non-positive `keep_n` /
    `keep_days` contribute nothing; a negative hard cap is treated as "unbounded" (defensive —
    a degenerate config never silently inverts into deleting everything via overflow).
    """
    ordered = sorted(entries, key=lambda e: e.seq)
    n = len(ordered)

    retained_ids: set[str] = set()
    if settings.snapshot_keep_n > 0:
        for entry in ordered[max(0, n - settings.snapshot_keep_n) :]:
            retained_ids.add(entry.snapshot_id)
    if settings.snapshot_keep_days > 0:
        for entry in ordered:
            if _within_keep_days(entry.created_at, now, settings.snapshot_keep_days):
                retained_ids.add(entry.snapshot_id)

    retained = [e for e in ordered if e.snapshot_id in retained_ids]

    # Hard cap by count: keep the newest `hard_max_n`, prune the oldest.
    hard_n = settings.snapshot_hard_max_n
    if hard_n >= 0 and len(retained) > hard_n:
        retained = retained[len(retained) - hard_n :]

    # Hard cap by bytes: drop oldest until the retained total fits the budget.
    hard_bytes = settings.snapshot_hard_max_bytes
    if hard_bytes >= 0:
        total = sum(e.size_bytes for e in retained)
        cut = 0
        while total > hard_bytes and cut < len(retained):
            total -= retained[cut].size_bytes
            cut += 1
        retained = retained[cut:]

    return {e.snapshot_id for e in retained}


def _prune_orphaned_operations(
    root: Path, doc_id: str, retained_snapshot_ids: set[str]
) -> list[str]:
    """Delete Operation Records whose linked snapshot is no longer retained (§6).

    A record is orphaned iff its `snapshot_id` is set AND that snapshot was pruned. Records that
    reference a still-retained snapshot, or reference no snapshot at all (`snapshot_id is None`),
    are kept. Unparseable / unreadable record files are left untouched. Returns the pruned ids.
    """
    ops_dir = sandbox.operations_dir(root, doc_id)
    if not ops_dir.is_dir():
        return []
    pruned: list[str] = []
    for path in sorted(ops_dir.glob("op_*.json")):
        try:
            record = OperationRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        snap = record.snapshot_id
        if snap is None or snap in retained_snapshot_ids:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            continue
        pruned.append(record.operation_id)
    return pruned


def prune_snapshots_at(
    root: Path,
    doc_id: str,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> PruneResult:
    """Prune one document's snapshots (path-based, no registry lookup) per the §6 policy.

    Reads the snapshot index, computes the retained set, deletes each pruned snapshot FILE (with
    a basename guard so a tampered manifest entry with a path component is skipped rather than
    followed), rewrites the index to the survivors, then prunes orphaned Operation Records. The
    working copy and `original.svg` are never touched. Idempotent: a second call with the same
    policy prunes nothing.
    """
    s = settings if settings is not None else get_settings()
    stamp = now if now is not None else datetime.now(UTC)

    entries = _read_index(root, doc_id)
    if not entries:
        return PruneResult(
            doc_id=doc_id,
            pruned_snapshot_ids=[],
            pruned_operation_ids=[],
            retained_count=0,
            freed_bytes=0,
        )

    retained_ids = _select_retained(entries, s, stamp)
    snap_dir = sandbox.snapshots_dir(root, doc_id)

    kept: list[SnapshotInfo] = []
    pruned_snap_ids: list[str] = []
    freed = 0
    for entry in entries:
        if entry.snapshot_id in retained_ids:
            kept.append(entry)
            continue
        # Candidate for pruning. Refuse a tampered basename outright (never delete via a path
        # component); keep such an entry indexed so the manifest never points at a live file.
        if Path(entry.file).name != entry.file:
            kept.append(entry)
            continue
        target = snap_dir / entry.file
        try:
            size = target.stat().st_size
        except FileNotFoundError:
            size = 0
        except OSError:
            kept.append(entry)
            continue
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            kept.append(entry)
            continue
        freed += size
        pruned_snap_ids.append(entry.snapshot_id)

    if pruned_snap_ids:
        _write_index(root, doc_id, kept)

    retained_snapshot_ids = {e.snapshot_id for e in kept}
    pruned_op_ids = _prune_orphaned_operations(root, doc_id, retained_snapshot_ids)

    if pruned_snap_ids or pruned_op_ids:
        log_file_io(
            _logger,
            action="prune_snapshots",
            doc_id=doc_id,
            pruned_snapshots=len(pruned_snap_ids),
            pruned_operations=len(pruned_op_ids),
            freed_bytes=freed,
            retained=len(kept),
        )

    return PruneResult(
        doc_id=doc_id,
        pruned_snapshot_ids=pruned_snap_ids,
        pruned_operation_ids=pruned_op_ids,
        retained_count=len(kept),
        freed_bytes=freed,
    )


def prune_document(
    doc_id: str,
    registry: Registry | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> PruneResult:
    """Prune one registered document's snapshots, resolving its root via the registry.

    Also prunes the document root's loop/live frames in the SAME explicit pass, reported on
    the result's `live_frames`. Raises `KeyError` if `doc_id` is unknown (propagated from registry).
    """
    reg = registry if registry is not None else get_registry()
    entry = reg.get(doc_id)
    root = Path(entry.root)
    result = prune_snapshots_at(root, doc_id, settings, now=now)
    result.live_frames = prune_live_frames_at(root, settings, now=now)
    return result


#: Glob for the rasterized loop/perceive frames `render_live_view` mints. Only these are
#: pruned by live-frame retention; other live artifacts (diff overlays, selection exports) are left
#: alone, and any frame still referenced by a Live Operation Record is excluded so a record's
#: before/after preview is never orphaned by the sweep.
_LIVE_FRAME_GLOB = "live-view-*.png"


def _referenced_live_frames(root: Path) -> set[str]:
    """Basenames of live frames referenced by any Live Operation Record under `root`.

    Reads each `op_*.json` record and collects the basenames of its before/after `previews` and
    `diff_artifacts` paths so live-frame pruning never deletes a frame an existing record points at.
    Unreadable records are skipped (their frames are simply not protected, never followed)."""
    ops_dir = sandbox.live_operations_dir(root)
    if not ops_dir.is_dir():
        return set()
    referenced: set[str] = set()
    for path in ops_dir.glob("op_*.json"):
        try:
            record = LiveOperationRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for val in record.previews.values():
            if isinstance(val, str) and val:
                referenced.add(Path(val).name)
        for val in record.diff_artifacts:
            if isinstance(val, str) and val:
                referenced.add(Path(val).name)
    return referenced


def prune_live_frames_at(
    root: Path,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> LivePruneResult:
    """Prune loop/live frames under one root's live artifacts dir (explicit only).

    Deletes `live-view-*.png` frames older than `live_frame_keep_days`, then trims the newest-kept
    survivors to fit `live_frame_max_bytes` (dropping oldest first). Frames still referenced by a
    Live Operation Record (before/after/diff) are NEVER pruned, so a record's preview is never
    orphaned. Only regular files directly under the sandbox-derived live artifacts dir are touched —
    never a client-supplied path. Non-positive `keep_days` ⇒ age never prunes; negative
    `live_frame_max_bytes` ⇒ the byte cap is treated as unbounded (defensive). View-only — no
    document mutation, no Operation Record. Idempotent.
    """
    s = settings if settings is not None else get_settings()
    stamp = now if now is not None else datetime.now(UTC)
    art_dir = sandbox.live_artifacts_dir(root)
    if not art_dir.is_dir():
        return LivePruneResult()

    protected = _referenced_live_frames(root)
    # Collect (path, mtime, size) for every loop frame, newest first.
    frames: list[tuple[Path, float, int]] = []
    for path in art_dir.glob(_LIVE_FRAME_GLOB):
        if not path.is_file() or path.name in protected:
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        frames.append((path, st.st_mtime, st.st_size))
    frames.sort(key=lambda f: f[1], reverse=True)

    keep_days = s.live_frame_keep_days
    cutoff = stamp - timedelta(days=keep_days) if keep_days > 0 else None
    # Non-positive caps mean "disabled" (keep everything) — the safe direction for retention. A 0
    # byte cap must NOT mean "delete every frame", so both bounds use a strict `> 0` guard.
    max_bytes = s.live_frame_max_bytes

    pruned = 0
    freed = 0
    retained = 0
    running = 0
    for path, mtime, size in frames:
        too_old = cutoff is not None and datetime.fromtimestamp(mtime, tz=UTC) < cutoff
        over_budget = max_bytes > 0 and running + size > max_bytes
        if too_old or over_budget:
            try:
                path.unlink()
            except FileNotFoundError:
                # Already gone — count it pruned, but reclaimed nothing now (don't inflate freed).
                pruned += 1
                continue
            except OSError:
                retained += 1
                running += size
                continue
            pruned += 1
            freed += size
        else:
            retained += 1
            running += size

    if pruned:
        log_file_io(
            _logger,
            action="prune_live_frames",
            pruned_frames=pruned,
            freed_bytes=freed,
            retained=retained,
        )
    return LivePruneResult(pruned_frames=pruned, freed_bytes=freed, retained_frames=retained)


def sweep_all_roots(settings: Settings | None = None, now: datetime | None = None) -> SweepResult:
    """Sweep every on-disk document under every configured root (the boot-time pass).

    Enumerates real `documents/<doc_id>/` directories that carry a `snapshots/index.json` and
    prunes each — independent of the in-process registry, so a freshly booted server still
    reclaims superseded snapshots from prior sessions. A single document's failure is logged and
    skipped; the sweep never aborts and never touches a working copy or original.
    """
    s = settings if settings is not None else get_settings()
    stamp = now if now is not None else datetime.now(UTC)

    results: list[PruneResult] = []
    live_results: list[LivePruneResult] = []
    for root in s.workspace_roots:
        docs_dir = sandbox.documents_dir(root)
        if docs_dir.is_dir():
            for child in sorted(docs_dir.iterdir()):
                if not child.is_dir():
                    continue
                if not sandbox.snapshots_index(root, child.name).is_file():
                    continue
                try:
                    results.append(prune_snapshots_at(root, child.name, s, now=stamp))
                except Exception:  # pragma: no cover - one bad doc never aborts the sweep
                    _logger.exception("snapshot prune failed for a document")
        #: loop/live frames fold into the SAME explicit sweep (never a mutating-tool side
        # effect). A live-frame prune failure is logged and skipped, never aborting the sweep.
        try:
            live_results.append(prune_live_frames_at(root, s, now=stamp))
        except Exception:  # pragma: no cover - one bad root never aborts the sweep
            _logger.exception("live-frame prune failed for a root")

    return SweepResult(
        documents=results,
        pruned_snapshots=sum(len(r.pruned_snapshot_ids) for r in results),
        pruned_operations=sum(len(r.pruned_operation_ids) for r in results),
        freed_bytes=sum(r.freed_bytes for r in results),
        live_frames=live_results,
        pruned_live_frames=sum(r.pruned_frames for r in live_results),
        freed_live_bytes=sum(r.freed_bytes for r in live_results),
    )
