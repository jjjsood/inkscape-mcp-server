"""Shared scaffolding for the E16-05 ``*_set`` collection tools.

The set tools (`export_set`, `optimize_set`, `quality_report_set`) each COMPOSE their existing
single-doc engine over a SET of documents and add the SAME three things on top: per-doc results, an
aggregate, and a cross-doc CONSISTENCY VERDICT. This module holds the iteration + verdict
scaffolding they share so each tool stays a thin composer (no fork of the per-doc engine, no
duplicated set-iteration logic).

`build_set_verdict` parses each document's working copy ONCE (safe parse), extracts its viewBox /
stroke-width / id-naming signals (:func:`inkscape_mcp.edit.collection.consistency_signals`), and
assembles the structured :class:`ConsistencyVerdict`. All errors map to stable, host-path-free
`ToolError`s (sec.12).
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError

from inkscape_mcp.document.inspect import DocumentNotFound, InspectionError, _load_tree
from inkscape_mcp.edit.collection import (
    ConsistencySignals,
    ConsistencyVerdict,
    build_consistency_verdict,
    consistency_signals,
)

#: Stable message for an empty `doc_ids` list — every set tool requires at least one document.
EMPTY_SET_MESSAGE = "doc_ids must contain at least one document id"


def require_unique_doc_ids(doc_ids: list[str]) -> list[str]:
    """Validate a `doc_ids` set: non-empty and free of duplicates; return it unchanged.

    A duplicate id would double-count a document in the aggregate and emit two snapshots for one
    mutating set op, so it is rejected with a stable, host-path-free message (sec.12).
    """
    if not doc_ids:
        raise ToolError(EMPTY_SET_MESSAGE)
    if len(set(doc_ids)) != len(doc_ids):
        raise ToolError("doc_ids must not contain duplicate document ids")
    return doc_ids


def collect_signals(doc_ids: list[str]) -> dict[str, ConsistencySignals]:
    """Parse each document's working copy once and return its consistency signals, keyed by doc_id.

    Raises a stable `ToolError` for an unknown id or an unparseable working copy (sec.12).
    """
    signals: dict[str, ConsistencySignals] = {}
    for doc_id in doc_ids:
        try:
            _entry, root = _load_tree(doc_id)
        except DocumentNotFound as exc:
            raise ToolError("document id not found") from exc
        except InspectionError as exc:
            raise ToolError("document could not be parsed safely") from exc
        signals[doc_id] = consistency_signals(root)
    return signals


def build_set_verdict(doc_ids: list[str]) -> ConsistencyVerdict:
    """Build the cross-doc :class:`ConsistencyVerdict` for a set of documents.

    Convenience composing :func:`collect_signals` + :func:`build_consistency_verdict`; used by every
    `*_set` tool so the verdict shape is IDENTICAL across export / optimize / quality sets.
    """
    signals = collect_signals(doc_ids)
    return build_consistency_verdict(
        viewboxes={d: s.viewbox for d, s in signals.items()},
        stroke_widths={d: s.stroke_width for d, s in signals.items()},
        id_namings={d: s.id_naming for d, s in signals.items()},
    )
