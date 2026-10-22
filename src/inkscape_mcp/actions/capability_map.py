"""Versioned Action capability map + discovery (architecture §3.5).

Records WHICH Inkscape Actions exist for a DETECTED Inkscape version. The execution layer
consults this map before assembling any ``--actions`` argv, so a missing Action is refused cleanly
instead of being handed to a host that does not implement it. Version tolerance per §3.5: NEVER
assume an Action exists — the map is built from the live ``inkscape --action-list`` probe.

Discovery itself is read-only (low risk): :func:`discover_actions` returns the host's probed Action
surface (and the allowlisted subset); :func:`build_action_map` derives the version-keyed map from
the same probe.

Pure functions only — no MCP decorators. The tools layer wraps these.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from inkscape_mcp.config import Settings, get_settings
from inkscape_mcp.runtime.probe import Capabilities, probe_capabilities

#: Unknown-version sentinel used when the probe could not parse a version string.
UNKNOWN_VERSION = "unknown"


class ActionCapabilityMap(BaseModel):
    """Version-keyed snapshot of the host's Action surface.

    `inkscape_version` is the raw probe version string (or `UNKNOWN_VERSION`); `actions` is the full
    set of Action ids the host reported via `inkscape --action-list`. Consulted by the chain layer
    to confirm an Action is present on THIS Inkscape before it is run.
    """

    inkscape_version: str
    actions: list[str] = Field(default_factory=list)

    def has(self, action_id: str) -> bool:
        """Whether `action_id` is present in this version's recorded Action surface."""
        return action_id in self.actions


class ActionDiscovery(BaseModel):
    """Read-only result of :func:`discover_actions` (the `list_actions` tool's output schema).

    Surfaces the host's actual Action surface (`actions`), the server-side `allowlisted` subset that
    may ever execute, and the `available` items (allowlisted AND present on this host). `notes`
    carries the probe's degradation messages (e.g. Inkscape absent) verbatim so the agent can branch
    on a degraded host without crashing.
    """

    inkscape_available: bool
    inkscape_version: str | None = None
    actions: list[str] = Field(default_factory=list)
    action_count: int = 0
    allowlisted: list[str] = Field(default_factory=list)
    available: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ExtensionDiscovery(BaseModel):
    """Read-only result of :func:`discover_extensions` (the `discover_extensions` tool output).

    Extension enumeration is best-effort and host-dependent; in the MVP no extension is enabled for
    execution (the default extension allowlist is empty). This reports the server-side
    `allowlisted` extension ids so an operator/agent can see exactly what — if anything — is
    permitted, plus any `notes` from the probe.
    """

    inkscape_available: bool
    inkscape_version: str | None = None
    allowlisted: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def build_action_map(capabilities: Capabilities | None = None) -> ActionCapabilityMap:
    """Build a version-keyed Action capability map from the runtime probe.

    Uses the supplied `capabilities` or runs a fresh probe. The map records the host's full Action
    surface keyed by the detected version string (or `UNKNOWN_VERSION`). Never assumes an Action
    exists — what the probe did not report is simply absent.
    """
    caps = capabilities if capabilities is not None else probe_capabilities()
    return ActionCapabilityMap(
        inkscape_version=caps.inkscape_version or UNKNOWN_VERSION, actions=list(caps.actions)
    )


def discover_actions(
    capabilities: Capabilities | None = None,
    settings: Settings | None = None,
) -> ActionDiscovery:
    """Return the host's probed Action surface + the allowlisted/available subsets (read-only).

    Does not mutate or assume; reflects exactly what the probe reported. `available` is the
    intersection of the server-side allowlist and the host's actual Action set — i.e. what could
    actually run through a validated chain on this host.
    """
    s = settings if settings is not None else get_settings()
    caps = capabilities if capabilities is not None else probe_capabilities()
    host_actions = set(caps.actions)
    allowlisted = sorted(s.action_allowlist)
    available = sorted(a for a in s.action_allowlist if a in host_actions)
    return ActionDiscovery(
        inkscape_available=caps.inkscape_available,
        inkscape_version=caps.inkscape_version,
        actions=list(caps.actions),
        action_count=len(caps.actions),
        allowlisted=allowlisted,
        available=available,
        notes=list(caps.notes),
    )


def discover_extensions(
    capabilities: Capabilities | None = None,
    settings: Settings | None = None,
) -> ExtensionDiscovery:
    """Return the server-side allowlisted extension set + probe notes (read-only, best-effort).

    Extension enumeration on the host is limited and version-dependent; in the MVP the execution
    surface is the allowlist (empty by default), so this reports what — if anything — an operator
    has opted in. No extension executes from this tool; it is diagnostic only.
    """
    s = settings if settings is not None else get_settings()
    caps = capabilities if capabilities is not None else probe_capabilities()
    allowlisted = sorted(s.extension_allowlist)
    notes = list(caps.notes)
    if not allowlisted:
        # S4: an empty allowlist is the default, not a probe failure — say so explicitly
        # so an agent doesn't read `allowlisted: []` as "extensions unavailable".
        notes.append(
            "extension execution is opt-in and OFF by default: the allowlist is empty until an "
            "operator adds extension ids to `extension_allowlist`; no extension can execute while "
            "it is empty (sec.12)."
        )
    return ExtensionDiscovery(
        inkscape_available=caps.inkscape_available,
        inkscape_version=caps.inkscape_version,
        allowlisted=allowlisted,
        notes=notes,
    )
