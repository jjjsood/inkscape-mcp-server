"""Live Operation Record tests (E4-02): approval gate, persistence, listing, gate posture."""

from __future__ import annotations

from pathlib import Path

import pytest

from inkscape_mcp.config import Settings
from inkscape_mcp.live.records import (
    LiveRecordError,
    clear_live_operations,
    get_live_operation,
    list_live_operations,
    new_live_operation,
    update_live_operation,
)
from inkscape_mcp.live.transport import LiveDocumentRef
from inkscape_mcp.operations import OperationStatus
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.risk import PolicyViolation, RiskClass


def _settings(tmp_path: Path) -> Settings:
    return Settings(workspace_roots=[tmp_path], live_enabled=True)


def test_high_risk_live_op_refused_without_approval(tmp_path: Path) -> None:
    # The approval gate fires BEFORE any record lands — never mutate unapproved (X1).
    with pytest.raises(PolicyViolation):
        new_live_operation(
            tool="live_apply_to_selection",
            risk_class=RiskClass.HIGH,
            params={},
            approval_token=None,
            settings=_settings(tmp_path),
        )
    assert not list(sandbox.live_operations_dir(tmp_path).glob("op_*.json"))


def test_high_risk_live_op_recorded_with_approval(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    record = new_live_operation(
        tool="live_apply_to_selection",
        risk_class=RiskClass.HIGH,
        params={"style": {"fill": "#ff0000"}},
        transport="fake",
        document=LiveDocumentRef(name="live.svg", path="/tmp/live.svg", object_count=1),
        selection=["r1"],
        approval_token="ok",
        settings=s,
    )
    assert record.status is OperationStatus.PROPOSED
    assert record.policy_decision["approval_required"] is True
    assert record.policy_decision["approved"] is True
    assert record.transport == "fake"
    assert record.selection == ["r1"]

    # Persisted and reloadable by id.
    loaded = get_live_operation(record.operation_id, settings=s)
    assert loaded.operation_id == record.operation_id

    # Lifecycle transition + provenance fields persist.
    applied = update_live_operation(
        record,
        settings=s,
        status=OperationStatus.APPLIED,
        affected_ids=["r1"],
        undo_friendly=True,
        previews={"before": "b.png", "after": "a.png"},
    )
    assert applied.status is OperationStatus.APPLIED
    assert applied.affected_ids == ["r1"]
    assert applied.previews == {"before": "b.png", "after": "a.png"}


def test_list_live_operations_newest_first(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    for _ in range(3):
        new_live_operation(
            tool="live_insert_svg",
            risk_class=RiskClass.HIGH,
            params={},
            approval_token="ok",
            settings=s,
        )
    log = list_live_operations(settings=s)
    assert log.count == 3
    assert len(log.operations) == 3


def test_no_workspace_root_refuses_to_record(tmp_path: Path) -> None:
    s = Settings(workspace_roots=[], live_enabled=True)
    with pytest.raises(LiveRecordError):
        new_live_operation(
            tool="live_apply_to_selection",
            risk_class=RiskClass.HIGH,
            params={},
            approval_token="ok",
            settings=s,
        )
    # The listing degrades cleanly (no root → empty, not an error).
    assert list_live_operations(settings=s).count == 0


def test_unknown_live_operation_id_raises_keyerror(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        get_live_operation("op_deadbeef", settings=_settings(tmp_path))


# --- E10-03: host-path-free / opaque document paths in records --------------------


def test_external_document_path_persisted_as_opaque_not_host_path(tmp_path: Path) -> None:
    # The running Inkscape reports the user's own on-disk path (a host path OUTSIDE the workspace).
    # It must never be persisted verbatim — the record stores an opaque marker, not the host path.
    host_path = "/home/someone/private/secret-design.svg"
    s = _settings(tmp_path)
    record = new_live_operation(
        tool="live_apply_to_selection",
        risk_class=RiskClass.HIGH,
        params={},
        document=LiveDocumentRef(name="secret-design.svg", path=host_path, object_count=2),
        approval_token="ok",
        settings=s,
    )
    assert record.document is not None
    assert record.document.path == "<external>"
    assert record.document.name == "secret-design.svg"  # the base name is still useful + safe

    # And the persisted JSON on disk carries no host path either.
    raw = sandbox.live_operations_dir(tmp_path).joinpath(f"{record.operation_id}.json").read_text()
    assert host_path not in raw
    assert "/home/someone" not in raw


def test_in_workspace_document_path_persisted_workspace_relative(tmp_path: Path) -> None:
    # A live doc that happens to live UNDER the workspace root is stored workspace-relative, never
    # as the absolute host path.
    s = _settings(tmp_path)
    in_ws = tmp_path / "sub" / "live.svg"
    record = new_live_operation(
        tool="live_insert_svg",
        risk_class=RiskClass.HIGH,
        params={},
        document=LiveDocumentRef(name="live.svg", path=str(in_ws), object_count=1),
        approval_token="ok",
        settings=s,
    )
    assert record.document is not None
    assert record.document.path == "sub/live.svg"
    assert not Path(record.document.path).is_absolute()


def test_list_live_operations_contains_no_host_path(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    new_live_operation(
        tool="live_apply_to_selection",
        risk_class=RiskClass.HIGH,
        params={},
        document=LiveDocumentRef(name="x.svg", path="/var/host/x.svg", object_count=1),
        approval_token="ok",
        settings=s,
    )
    blob = list_live_operations(settings=s).model_dump_json()
    assert "/var/host" not in blob
    assert "/var/host/x.svg" not in blob


def test_clear_live_operations_removes_persisted_records(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    for _ in range(3):
        new_live_operation(
            tool="live_insert_svg",
            risk_class=RiskClass.HIGH,
            params={},
            approval_token="ok",
            settings=s,
        )
    assert list_live_operations(settings=s).count == 3

    removed = clear_live_operations(settings=s)
    assert removed == 3
    assert list_live_operations(settings=s).count == 0
    assert not list(sandbox.live_operations_dir(tmp_path).glob("op_*.json"))


def test_clear_live_operations_idempotent_and_no_root_safe(tmp_path: Path) -> None:
    # Clearing an already-empty store is a clean no-op, and no workspace root degrades to 0.
    assert clear_live_operations(settings=_settings(tmp_path)) == 0
    no_root = Settings(workspace_roots=[], live_enabled=True)
    assert clear_live_operations(settings=no_root) == 0
