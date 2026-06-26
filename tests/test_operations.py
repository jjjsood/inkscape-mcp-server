"""Operation Record scaffold tests (E1-09 / ADR-004)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, Settings, get_settings
from inkscape_mcp.operations import (
    OperationStatus,
    get_operation,
    new_operation,
    update_operation,
)
from inkscape_mcp.registry import Registry
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.risk import PolicyViolation, RiskClass, enforce_risk_policy

SVG_BODY = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'


@pytest.fixture
def reg_with_doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Registry, str, Settings]:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(root))
    get_settings.cache_clear()
    settings = get_settings()
    src = root / "logo.svg"
    src.write_bytes(SVG_BODY)
    reg = Registry(settings)
    entry = reg.open_document(str(src))
    return reg, entry.doc_id, settings


def test_new_operation_writes_proposed_record(
    reg_with_doc: tuple[Registry, str, Settings],
) -> None:
    reg, doc_id, settings = reg_with_doc
    rec = new_operation(
        doc_id,
        tool="set_style",
        risk_class=RiskClass.MEDIUM,
        params={"fill": "red"},
        registry=reg,
    )
    assert rec.operation_id.startswith("op_")
    assert rec.status is OperationStatus.PROPOSED
    assert rec.risk_class is RiskClass.MEDIUM
    assert rec.params == {"fill": "red"}

    root = settings.workspace_roots[0]
    op_file = sandbox.operations_dir(root, doc_id) / f"{rec.operation_id}.json"
    assert op_file.is_file()
    on_disk = json.loads(op_file.read_text())
    assert on_disk["status"] == "proposed"
    assert on_disk["tool"] == "set_style"


def test_update_operation_transitions_and_links_snapshot(
    reg_with_doc: tuple[Registry, str, Settings],
) -> None:
    reg, doc_id, _ = reg_with_doc
    rec = new_operation(
        doc_id, tool="set_style", risk_class=RiskClass.MEDIUM, params={}, registry=reg
    )
    updated = update_operation(
        rec,
        registry=reg,
        status=OperationStatus.APPLIED,
        snapshot_id="0001-op_x",
    )
    assert updated.status is OperationStatus.APPLIED
    assert updated.snapshot_id == "0001-op_x"
    assert updated.updated_at >= rec.updated_at
    assert updated.operation_id == rec.operation_id


def test_get_operation_round_trips(
    reg_with_doc: tuple[Registry, str, Settings],
) -> None:
    reg, doc_id, _ = reg_with_doc
    rec = new_operation(
        doc_id,
        tool="set_style",
        risk_class=RiskClass.HIGH,
        params={"k": 1},
        registry=reg,
        approval_token="approved-once",
    )
    update_operation(
        rec, registry=reg, status=OperationStatus.APPLIED, artifacts=["preview-after.png"]
    )
    loaded = get_operation(doc_id, rec.operation_id, registry=reg)
    assert loaded.operation_id == rec.operation_id
    assert loaded.status is OperationStatus.APPLIED
    assert loaded.artifacts == ["preview-after.png"]
    assert loaded.risk_class is RiskClass.HIGH


def test_get_operation_unknown_raises(
    reg_with_doc: tuple[Registry, str, Settings],
) -> None:
    reg, doc_id, _ = reg_with_doc
    with pytest.raises(KeyError):
        get_operation(doc_id, "op_missing", registry=reg)


def test_get_operation_rejects_traversal_id(
    reg_with_doc: tuple[Registry, str, Settings],
) -> None:
    # A non-conforming operation_id must never be used to build a path (traversal guard).
    reg, doc_id, _ = reg_with_doc
    with pytest.raises(KeyError):
        get_operation(doc_id, "../../../etc/passwd", registry=reg)


def test_enforce_risk_policy_gates() -> None:
    assert enforce_risk_policy(RiskClass.LOW)["permitted"] is True
    assert enforce_risk_policy(RiskClass.MEDIUM)["approved"] is True
    with pytest.raises(PolicyViolation):
        enforce_risk_policy(RiskClass.RESTRICTED)
    with pytest.raises(PolicyViolation):
        enforce_risk_policy(RiskClass.HIGH)
    decision = enforce_risk_policy(RiskClass.HIGH, approval_token="approved-once")
    assert decision["approved"] is True
    assert decision["approval_required"] is True


def test_new_operation_enforces_policy(
    reg_with_doc: tuple[Registry, str, Settings],
) -> None:
    reg, doc_id, _ = reg_with_doc
    with pytest.raises(PolicyViolation):
        new_operation(doc_id, tool="t", risk_class=RiskClass.RESTRICTED, params={}, registry=reg)
    with pytest.raises(PolicyViolation):
        new_operation(doc_id, tool="t", risk_class=RiskClass.HIGH, params={}, registry=reg)
    rec = new_operation(doc_id, tool="t", risk_class=RiskClass.MEDIUM, params={}, registry=reg)
    assert rec.policy_decision["permitted"] is True
