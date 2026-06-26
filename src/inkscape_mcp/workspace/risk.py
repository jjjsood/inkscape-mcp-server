"""Risk classes (project API rules / workspace model).

Every tool call carries a `RiskClass`; the policy layer splits behavior on it:
- low: read / render
- medium: write-new / style / text / transform
- high: overwrite / delete / outline / paths / extensions
- restricted: code / network / fs
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class RiskClass(StrEnum):
    """Per-operation risk class applied to each tool invocation."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    RESTRICTED = "restricted"


class PolicyViolation(Exception):
    """An operation was refused by the risk policy.

    Carries a stable, host-path-free public message; tool/resource layers map it to a
    `ToolError`/`ResourceError`.
    """


def enforce_risk_policy(
    risk_class: RiskClass,
    *,
    approval_token: str | None = None,
) -> dict[str, Any]:
    """Gate an operation by risk class and return the recorded policy decision.

    Policy split (project risk model):
    - ``low`` / ``medium``: permitted (reversible, snapshot-backed).
    - ``high``: permitted only with an explicit per-operation ``approval_token`` (the
      token is minted/confirmed out of band, bound to a single operation — never an
      ambient env flag a model can request).
    - ``restricted``: never permitted (no code/network/fs-escape tools ship in the MVP).

    Raises ``PolicyViolation`` when the operation is not permitted. Returns a
    machine-readable decision dict for the Operation Record's ``policy_decision`` field.
    """
    # A whitespace-only token is not a real approval — treat it as absent (X1 hardening).
    has_approval = bool(approval_token and approval_token.strip())
    if risk_class is RiskClass.RESTRICTED:
        raise PolicyViolation("restricted operations are not permitted")
    if risk_class is RiskClass.HIGH and not has_approval:
        raise PolicyViolation("high-risk operation requires explicit approval")
    return {
        "risk_class": risk_class.value,
        "permitted": True,
        "approval_required": risk_class is RiskClass.HIGH,
        "approved": risk_class is not RiskClass.HIGH or has_approval,
    }
