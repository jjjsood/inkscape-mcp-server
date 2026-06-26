"""Path geometry engine (E6-01, ADR-005 — Inkscape ENGINE, not direct DOM).

Pure functions (no MCP decorators) that drive Inkscape Actions to perform DESTRUCTIVE path
geometry on the working copy, and a thin DOM-replace bridge so the result flows through the shared
E2-04 mutating pipeline (:func:`inkscape_mcp.edit.pipeline.apply_edit`) — keeping every path op
snapshotted, recorded, previewed, and reversible like any other edit.

Why the engine and not lxml (ADR-005): boolean ops, simplification, stroke outlining, and path
cleanup are real geometry that only Inkscape computes correctly. So each op:

1. validates the target object ids (safe SVG-id charset + must EXIST in the working copy),
2. runs ``inkscape <working-copy> --actions="select-by-id:<ids>;<action>" --export-type=svg
   --export-plain-svg --export-filename=<temp>`` via :func:`run_inkscape` (``shell=False``,
   arg lists only, per-process timeout enforced — sec.12),
3. parses the produced SVG bytes back through the normative safe parser, and
4. replaces the working tree's root in memory with the engine's result, so the pipeline writes it
   over the working copy, snapshots the pre-op state, and renders before/after previews.

The engine NEVER writes the working copy or the original itself — it emits to a private temp file
under the OS temp dir and hands the parsed bytes back to the pipeline, which owns all persistence.

SECURITY (sec.12 / X1):

- ARG LISTS ONLY. The action string is assembled from a FIXED action token (drawn from
  :data:`_ACTIONS`, never client input) plus comma-joined object ids that have each passed
  :func:`is_safe_object_id`; no shell string is ever built and no client value is interpolated
  except ids that match the conservative argv-safe charset.
- Object ids are validated TWICE: charset (argv-safe) and existence in the parsed working copy,
  before they reach ``select-by-id``.
- The input path is the registry's sandbox-validated ``working_path``; the output path is a
  server-created temp file (never client input). The temp file is always cleaned up.
- The produced SVG is re-parsed through the SAFE parser, so a malformed/unsafe engine output can
  never reach the working copy.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from lxml import etree

from inkscape_mcp.config import Settings, get_settings
from inkscape_mcp.edit.dom import (
    EditError,
    TargetNotFound,
    all_ids,
    color_key,
    find_by_id,
    parse_style,
    rewrite_references,
    set_style_property,
)
from inkscape_mcp.engine.ops import engine_mode_is_shell, engine_run_actions
from inkscape_mcp.engine.process import EngineError
from inkscape_mcp.logging_setup import get_logger
from inkscape_mcp.render.cli import is_safe_object_id
from inkscape_mcp.workspace.limits import LimitExceeded, check_output_size
from inkscape_mcp.workspace.subprocess_exec import ProcessError, ProcessResult, run_inkscape
from inkscape_mcp.workspace.xml_safety import UnsafeXMLError, parse_svg_bytes

_logger = get_logger("edit.paths")

#: Fixed Inkscape Action token for each path op. Values are NEVER client-supplied — they are the
#: only strings ever spliced into the ``--actions`` argv element (alongside validated object ids).
#: Verified present on Inkscape 1.4.3 via ``inkscape --action-list``.
SIMPLIFY = "simplify_path"
UNION = "boolean_union"
DIFFERENCE = "boolean_difference"
INTERSECTION = "boolean_intersection"
EXCLUSION = "boolean_exclusion"
COMBINE = "combine_paths"
BREAK_APART = "break_apart"
STROKE_TO_PATH = "stroke_to_path"
CLEANUP = "cleanup_paths"

#: op token -> Inkscape Action name (the verb run after ``select-by-id``).
_ACTIONS: dict[str, str] = {
    SIMPLIFY: "path-simplify",
    UNION: "path-union",
    DIFFERENCE: "path-difference",
    INTERSECTION: "path-intersection",
    EXCLUSION: "path-exclusion",
    COMBINE: "path-combine",
    BREAK_APART: "path-break-apart",
    STROKE_TO_PATH: "object-stroke-to-path",
    # cleanup is a DELIBERATE alias of simplify: Inkscape 1.4.3 has no distinct lossless cleanup
    # Action over --actions (to-absolute no-ops headless; clean-up-path is a 3rd-party extension we
    # do not exec). Both run path-simplify (lossy node-reduction). See `cleanup_paths` docstring.
    CLEANUP: "path-simplify",
}

#: Ops that operate on a SET of paths and need at least two distinct targets to mean anything.
_BINARY_OPS = frozenset({UNION, DIFFERENCE, INTERSECTION, EXCLUSION, COMBINE})

#: Merge ops that collapse 2+ targets into ONE result element. For these the engine guarantees a
#: single, documented surviving id (see :func:`_surviving_target_id`): the BOTTOM-most target — the
#: one painted first, i.e. first in document order. Inkscape's ``path-union`` / ``path-difference``
#: already keep the bottom id, but ``path-combine`` keeps the TOP id (last in document order); the
#: engine normalizes that case so all merge ops survive the SAME (bottom) id. (E10-07 / E11-07.)
_MERGE_OPS = frozenset({UNION, DIFFERENCE, INTERSECTION, EXCLUSION, COMBINE})


class PathOpError(Exception):
    """A path geometry op failed (engine error, timeout, or empty/unsafe output).

    Carries a stable, host-path-free public message; the tool layer maps it to `ToolError`.
    """


def _settings(settings: Settings | None) -> Settings:
    return settings if settings is not None else get_settings()


def validate_targets(root: etree._Element, object_ids: list[str], op: str) -> list[str]:
    """Validate the target ids for `op`: non-empty, argv-safe charset, and present in the document.

    Returns the validated id list (order preserved, duplicates removed). Raises
    :class:`EditError` for an empty list, an unsafe id token, or a count that cannot satisfy the
    op (binary/set ops need >= 2 distinct targets), and :class:`TargetNotFound` for an id that is
    not present in the working copy. Validation runs entirely on the parsed tree BEFORE any
    Inkscape invocation, so a bad id never reaches argv.
    """
    if not object_ids:
        raise EditError("no target object ids supplied")

    # De-dupe while preserving order (a repeated id is meaningless to a boolean op).
    seen: dict[str, None] = {}
    for oid in object_ids:
        seen.setdefault(oid, None)
    ids = list(seen)

    for oid in ids:
        if not is_safe_object_id(oid):
            raise EditError("object id is not a safe svg id")
        # ':' is the Inkscape --actions "verb:argument" delimiter; reject it in a select-by-id
        # target so a namespaced/crafted id can never perturb the action grammar (defence in depth).
        if ":" in oid:
            raise EditError("object id must not contain ':'")

    present = all_ids(root)
    for oid in ids:
        if oid not in present:
            raise TargetNotFound("object id not found in document")

    if op in _BINARY_OPS and len(ids) < 2:
        raise EditError(f"{op} requires at least two distinct object ids")

    return ids


def _bottom_target_id(root: etree._Element, ids: list[str]) -> str | None:
    """Return the validated target id that appears FIRST in document order (the bottom element).

    SVG paints in document order, so the element earliest in the tree is the bottom-most. That is
    the documented surviving id for a merge op (E10-07 standardizes combine/boolean on it). `ids`
    are already validated to exist; returns ``None`` only if none are found (never expected).
    """
    wanted = set(ids)
    for elem in root.iter():
        if isinstance(elem.tag, str):
            eid = elem.get("id")
            if eid in wanted:
                return eid
    return None


def _normalize_surviving_id(new_root: etree._Element, ids: list[str], bottom_id: str) -> str | None:
    """Make the merge result carry `bottom_id`, and return the id actually present afterwards.

    A merge collapses the targets to ONE element. Whichever of the original target ids still
    exists in the engine output is that result element. If it is already `bottom_id` (union /
    difference), nothing changes. If it is a different target id (``path-combine`` keeps the TOP
    id), the surviving element is renamed to `bottom_id` and intra-document references are rewritten
    so the standardized bottom id is what survives. Returns the surviving id, or ``None`` if no
    target id remains (the engine produced an unexpected shape — the caller then omits a result id
    rather than guessing).
    """
    present = all_ids(new_root)
    survivors = [oid for oid in ids if oid in present]
    if not survivors:
        return None
    survivor = bottom_id if bottom_id in present else survivors[0]
    if survivor != bottom_id and bottom_id not in present:
        for elem in new_root.iter():
            if isinstance(elem.tag, str) and elem.get("id") == survivor:
                elem.set("id", bottom_id)
                rewrite_references(new_root, {survivor: bottom_id})
                return bottom_id
    return survivor


def _effective_value(elem: etree._Element, prop: str) -> str | None:
    """Effective value of CSS `prop` on `elem`: inline-style declaration wins over the attribute."""
    decls = parse_style(elem)
    if prop in decls:
        return decls[prop]
    return elem.get(prop)


def _has_explicit_fill(elem: etree._Element) -> bool:
    """True if `elem` carries an explicit ``fill`` (inline style or presentation attribute)."""
    return "fill" in parse_style(elem) or elem.get("fill") is not None


def _outline_fill_from(elem: etree._Element | None) -> str:
    """Pick the explicit fill for a stroke-outlined result: the source's stroke colour, else black.

    ``object-stroke-to-path`` turns a stroked outline into a FILLED path, but Inkscape's
    ``--export-plain-svg`` output drops the paint and leaves the result with no explicit ``fill``,
    so it renders on the SVG default (black) instead of the original stroke colour (E13-04). The
    source element's effective stroke colour is the correct fill for the outline; fall back to
    ``#000000`` (the default it would otherwise rely on) when the source had no concrete stroke.
    """
    if elem is not None:
        stroke = _effective_value(elem, "stroke")
        if stroke is not None:
            token = stroke.strip()
            if token and token.lower() != "none":
                return token
    return "#000000"


def _restore_outline_fill(
    old_root: etree._Element, new_root: etree._Element, ids: list[str]
) -> None:
    """Give each stroke-to-path result path an EXPLICIT fill so it no longer relies on the default.

    Scoped to the target `ids` only (the converted elements keep their id), so unrelated paths
    that legitimately rely on the default fill are never touched. For each target that survives as
    a ``path`` with no explicit ``fill``, set ``fill`` to the source element's stroke colour (read
    from the pre-op tree `old_root`). E13-04.
    """
    for oid in ids:
        new_elem = find_by_id(new_root, oid)
        if new_elem is None or etree.QName(new_elem).localname != "path":
            continue
        if _has_explicit_fill(new_elem):
            continue
        fill = _outline_fill_from(find_by_id(old_root, oid))
        set_style_property(new_elem, "fill", fill, key=color_key)


def _is_empty_marker_container(elem: etree._Element) -> bool:
    """True iff `elem` is an empty marker container left behind by ``object-stroke-to-path``.

    When a stroked object carries no marker, ``object-stroke-to-path`` still emits an empty
    ``<g inkscape:label="markers">`` (or a bare empty ``<g>`` / ``<path>``) wrapping nothing — a
    no-op container the later ``svg_web_optimize`` pass strips. We drop it here so the conversion
    leaves a clean tree in one call (E16-10c). A container is "empty" only when it has NO element
    children AND, for a ``path``, no ``d`` geometry, so a real marker subtree is never removed.
    """
    if not isinstance(elem.tag, str):
        return False
    local = etree.QName(elem).localname
    if local == "g":
        # An empty group: no element children at all.
        return not any(isinstance(child.tag, str) for child in elem)
    if local == "path":
        # A degenerate marker path: no geometry to draw.
        d = (elem.get("d") or "").strip()
        return not d
    return False


def _drop_empty_markers(old_root: etree._Element, new_root: etree._Element, ids: list[str]) -> None:
    """Remove empty marker containers ``object-stroke-to-path`` leaves when a target has no marker.

    Scoped to NEWLY-INTRODUCED empty containers only: an element id present in the engine output
    but absent from the pre-op tree, that is an empty ``<g>`` / geometry-less ``<path>`` and is NOT
    one of the converted target ids. This never touches a real (non-empty) marker subtree, nor any
    element that already existed, so the only thing dropped is the no-op stub. E16-10c.
    """
    before = all_ids(old_root)
    target_ids = set(ids)
    for elem in list(new_root.iter()):
        if not isinstance(elem.tag, str):
            continue
        eid = elem.get("id")
        # Only consider freshly-introduced, non-target containers (a converted path keeps its id).
        if eid is not None and (eid in before or eid in target_ids):
            continue
        if not _is_empty_marker_container(elem):
            continue
        parent = elem.getparent()
        if parent is not None:
            parent.remove(elem)


def predicted_result_id(root: etree._Element, op: str, ids: list[str]) -> str | None:
    """The id a merge `op` WOULD standardize its result to (the bottom-most target), or ``None``.

    Read-only preview for the dry-run path: returns the first-in-document-order target id for a
    merge op (:data:`_MERGE_OPS`), and ``None`` for a non-merge op. `ids` must already be validated
    and present. No mutation, no engine call.
    """
    if op not in _MERGE_OPS:
        return None
    return _bottom_target_id(root, ids)


def _build_args(working_path: Path, out_path: Path, action: str, ids: list[str]) -> list[str]:
    """Build the Inkscape argv for one path op (arg list only — sec.12).

    The single ``--actions`` element is the ONLY place a value is composed, from the fixed action
    token and the already-validated, argv-safe ids; the in/out paths are server-controlled. No
    shell string is built.
    """
    action_chain = f"select-by-id:{','.join(ids)};{action}"
    return [
        str(working_path),
        f"--actions={action_chain}",
        "--export-type=svg",
        "--export-plain-svg",
        f"--export-filename={out_path}",
    ]


def run_path_op(
    working_path: Path,
    op: str,
    ids: list[str],
    settings: Settings | None = None,
) -> bytes:
    """Run one path op through Inkscape and return the mutated, safe-parsed SVG bytes.

    `op` must be a key of :data:`_ACTIONS`; `ids` must already be validated (argv-safe + present).
    The working copy is NOT modified here — Inkscape emits to a private temp file that is always
    removed, and the bytes are returned for the pipeline to persist. The output is re-parsed
    through the safe parser; a non-zero exit, timeout, missing/empty output, or unsafe XML raises
    :class:`PathOpError`.
    """
    action = _ACTIONS.get(op)
    if action is None:  # pragma: no cover - guarded by the tool layer
        raise PathOpError(f"unsupported path op: {op!r}")

    s = _settings(settings)

    # E12-04: when the warm shell engine is enabled, run the op on the stateful worker (reloads the
    # on-disk working copy first, so its document never diverges from disk) and return the result
    # bytes. ANY engine fault falls back to the per-call CLI below — correctness can never regress.
    if engine_mode_is_shell(s):
        action_chain = f"select-by-id:{','.join(ids)};{action}"
        try:
            return engine_run_actions(working_path, action_chain, settings=s)
        except EngineError as exc:
            _logger.warning(
                "warm engine path op fell back to per-call CLI",
                extra={"op": op, "error": type(exc).__name__},
            )

    # Server-created temp output under the OS temp dir (never client input). Closed immediately so
    # Inkscape owns the write; removed in the finally.
    fd, tmp_name = tempfile.mkstemp(prefix="inkscape-mcp-path-", suffix=".svg")
    out_path = Path(tmp_name)
    try:
        os.close(fd)
        args = _build_args(working_path, out_path, action, ids)
        try:
            result: ProcessResult = run_inkscape(args, settings=s)
        except ProcessError as exc:
            raise PathOpError("inkscape engine unavailable") from exc

        if result.timed_out:
            _logger.error("path op timed out", extra={"op": op, "duration_s": result.duration_s})
            raise PathOpError("path operation timed out")
        if result.returncode != 0 or not out_path.exists():
            _logger.error("path op failed", extra={"op": op, "returncode": result.returncode})
            raise PathOpError("path operation failed")

        # Bound the engine output before reading it into memory (same cap as render/export).
        try:
            check_output_size(out_path, s)
        except LimitExceeded as exc:
            raise PathOpError("path operation output exceeds size limit") from exc

        data = out_path.read_bytes()
        if not data.strip():
            raise PathOpError("path operation produced empty output")

        # Re-parse through the SAFE parser: a malformed/unsafe engine output never reaches disk.
        try:
            parse_svg_bytes(data)
        except UnsafeXMLError as exc:
            raise PathOpError("path operation produced unsafe output") from exc
        return data
    finally:
        try:
            out_path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best-effort cleanup
            pass


def apply_path_op(
    tree: etree._ElementTree,
    working_path: Path,
    op: str,
    object_ids: list[str],
    settings: Settings | None = None,
    result_holder: list[str] | None = None,
) -> str:
    """Pipeline `mutate` kernel: run `op` on the engine and replace the tree in memory.

    Used as the ``mutate(tree) -> str`` callback for :func:`apply_edit`. Validates the targets,
    runs the Inkscape engine on the on-disk working copy (which still holds the pre-op state when
    the pipeline calls this), parses the engine's SVG output, and REPLACES the working tree's root
    with the engine result so the pipeline serializes it over the working copy. Returns a short
    human summary. Raises :class:`EditError` / :class:`TargetNotFound` on bad input (so the
    pipeline discards the record before any write) and :class:`PathOpError` on an engine failure.

    For a MERGE op (union/difference/combine, :data:`_MERGE_OPS`) the result collapses to one
    element whose id is standardized to the BOTTOM-most target (first in document order) — see
    :func:`_normalize_surviving_id`. When `result_holder` is supplied, that surviving id is written
    into it (``result_holder[:] = [surviving_id]``) so the tool layer can return it as
    ``result_id`` / ``surviving_id`` without re-inspecting the document (E10-07 / E11-07).
    """
    root = tree.getroot()
    ids = validate_targets(root, object_ids, op)
    # Bottom (first-in-document) target — the documented surviving id — computed on the PRE-op tree.
    bottom_id = _bottom_target_id(root, ids) if op in _MERGE_OPS else None
    data = run_path_op(working_path, op, ids, settings)
    # Parse the engine output through the SAFE parser (defence in depth: `run_path_op` already
    # validated it, but the root we splice in must come from the safe parser, never the default).
    try:
        new_root = parse_svg_bytes(data).getroot()
    except UnsafeXMLError as exc:  # pragma: no cover - run_path_op already rejected unsafe output
        raise PathOpError("path operation produced unsafe output") from exc

    surviving_id: str | None = None
    if op in _MERGE_OPS and bottom_id is not None:
        surviving_id = _normalize_surviving_id(new_root, ids, bottom_id)
    if result_holder is not None and surviving_id is not None:
        result_holder[:] = [surviving_id]

    # E13-04: stroke_to_path outlines the stroke into a FILLED path, but the engine's plain-SVG
    # output leaves no explicit fill, so write the source stroke colour as the result's fill rather
    # than letting it fall through to the SVG default black.
    # E16-10c: it also leaves an empty marker container behind when the source had no marker — drop
    # that no-op stub here so the conversion lands a clean tree in one call (was stripped later by
    # svg_web_optimize).
    if op == STROKE_TO_PATH:
        _restore_outline_fill(root, new_root, ids)
        _drop_empty_markers(root, new_root, ids)

    # Replace the live tree root with the engine output so `write_working_tree` persists it.
    tree._setroot(new_root)

    action = _ACTIONS[op]
    return f"applied {op} (Inkscape {action}) to {len(ids)} object(s): {', '.join(ids)}"


def describe_dry_run(root: etree._Element, op: str, object_ids: list[str]) -> str:
    """Validate targets and return a human description of what `op` WOULD do (no mutation).

    Used by the dry-run path: it performs the same target validation as a real run (so a bad id
    fails the same way) but never invokes Inkscape and never touches the working copy.
    """
    ids = validate_targets(root, object_ids, op)
    action = _ACTIONS[op]
    return (
        f"dry-run: would apply {op} (Inkscape {action}) to {len(ids)} object(s): "
        f"{', '.join(ids)} — no change written"
    )


__all__ = [
    "BREAK_APART",
    "CLEANUP",
    "COMBINE",
    "DIFFERENCE",
    "EXCLUSION",
    "INTERSECTION",
    "SIMPLIFY",
    "STROKE_TO_PATH",
    "UNION",
    "PathOpError",
    "apply_path_op",
    "describe_dry_run",
    "predicted_result_id",
    "run_path_op",
    "validate_targets",
]
