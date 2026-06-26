"""Caller-resolvable artifact locations (E11-01 / folds E10-08 N9).

ONE LOCATION CONTRACT. Every artifact-producing tool returns two path fields — ``artifact_path``
and ``workspace_relative_path`` — that now carry the SAME value with a single, documented meaning:

    a POSIX path relative to the WORKSPACE ROOT (``settings.workspace_roots[0]``).

An agent opens any returned artifact by joining that value to the workspace root with NO ``find`` /
``stat`` first — a single, unambiguous join works for EVERY output, whether it landed in the managed
per-document dir (then the value carries the ``.inkscape-mcp/documents/<doc_id>/...`` base, so the
doc id is visible) or in a caller-chosen ``out_dir`` (then it is just the in-workspace relative
path to that dir). Both fields are kept for back-compat but mean exactly the same thing;
``artifact_path`` is retained only so older callers keep reading a populated field.

Neither field is ever an absolute host path (sec.12): no path returned by these helpers leaves the
sandbox or leaks a host filesystem prefix. The conversion is purely a re-anchor of the same
in-sandbox file to the workspace-root base.
"""

from __future__ import annotations

from pathlib import Path

from inkscape_mcp.registry import DocEntry


def workspace_relative_path(entry: DocEntry, artifact_path: str) -> str:
    """Re-anchor a per-doc ``artifact_path`` to the WORKSPACE ROOT.

    `artifact_path` is the POSIX path relative to the per-document workspace dir that the
    render/export engine computes for a managed output (e.g. ``artifacts/exports/<name>.png``). The
    result is the same file expressed relative to the owning workspace root, i.e.
    ``.inkscape-mcp/documents/<doc_id>/artifacts/exports/<name>.png`` — the single value placed in
    BOTH ``artifact_path`` and ``workspace_relative_path`` under the one location contract.

    Both `entry.workspace_dir` and `entry.root` are sandbox-internal real paths; the returned
    value is the relative segment between them joined with `artifact_path`, never an absolute
    host path. Raises `ValueError` if the workspace dir is not under the root (a construction
    bug — the per-doc dir is always created inside the root).
    """
    workspace_dir = Path(entry.workspace_dir)
    root = Path(entry.root)
    doc_rel = workspace_dir.relative_to(root)  # ValueError if not contained (construction bug)
    return (doc_rel / artifact_path).as_posix()
