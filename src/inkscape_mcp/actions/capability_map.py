"""Versioned Action capability map + discovery (E6-02 / architecture §3.5).

Records WHICH Inkscape Actions exist for a DETECTED Inkscape version, persisted append-style under
``<root>/.inkscape-mcp/action-maps/<version>.json`` (one file per version). The execution layer
consults this map before assembling any ``--actions`` argv, so a missing Action is refused cleanly
instead of being handed to a host that does not implement it. Version tolerance per §3.5: NEVER
assume an Action exists — the map is built from the live ``inkscape --action-list`` probe.

Discovery itself is read-only (low risk): :func:`discover_actions` returns the host's probed Action
surface (and the allowlisted subset) without touching disk; :func:`build_action_map` derives the
persisted, version-keyed map from the same probe.

Pure functions only — no MCP decorators. The tools layer wraps these.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from inkscape_mcp.config import Settings, get_settings
from inkscape_mcp.logging_setup import get_logger
from inkscape_mcp.runtime.probe import Capabilities, probe_capabilities
from inkscape_mcp.workspace import sandbox

logger = get_logger("actions.capability_map")

#: Unknown-version sentinel used as the map filename stem when the probe could not parse a version
#: string. Keeps the persisted map shape stable (always keyed) without inventing a version.
UNKNOWN_VERSION = "unknown"

#: A persisted-map filename stem (the version key) is constrained to a safe charset BEFORE any path
#: construction, so a crafted version string from the probe can never traverse out of the
#: ``action-maps/`` dir. Inkscape versions look like ``1.4.3``; we also allow the sentinel.
_VERSION_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ActionCapabilityMap(BaseModel):
    """Version-keyed snapshot of the host's Action surface (E6-02).

    `inkscape_version` is the raw probe version string (or `UNKNOWN_VERSION`); `actions` is the full
    set of Action ids the host reported via `inkscape --action-list`; `action_count` is its length.
    Persisted as ``action-maps/<version>.json`` and consulted by the chain layer to confirm an
    Action is present on THIS Inkscape before it is run.
    """

    inkscape_version: str
    inkscape_version_tuple: tuple[int, int, int] | None = None
    actions: list[str] = Field(default_factory=list)
    action_count: int = 0
    probed_at: str
    source: str = "probe"

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


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def _version_key(version: str | None) -> str:
    """Map a raw probe version string to a safe filename stem (or the unknown sentinel).

    The Inkscape ``--version`` line is verbose (``Inkscape 1.4.3 (0d15f75042, ...)``); we key the
    map on a compact, charset-validated token. An unparseable/unsafe string degrades to
    ``UNKNOWN_VERSION`` rather than producing an unsafe path component.
    """
    if not version:
        return UNKNOWN_VERSION
    match = re.search(r"(\d+\.\d+(?:\.\d+)?)", version)
    candidate = match.group(1) if match else version.strip()
    candidate = candidate.strip()
    if not candidate or not _VERSION_KEY_RE.match(candidate):
        return UNKNOWN_VERSION
    return candidate


def _map_path(root: Path, version_key: str) -> Path:
    """Resolve ``action-maps/<version>.json``, shape-validating the key first.

    The key is validated against `_VERSION_KEY_RE` before being used to build a path, so a crafted
    version can never traverse out of the ``action-maps/`` dir (mirrors the Operation Record id
    guard). Raises `KeyError` for an unsafe key.
    """
    if not _VERSION_KEY_RE.match(version_key):
        raise KeyError(version_key)
    return sandbox.action_maps_dir(root) / f"{version_key}.json"


def build_action_map(capabilities: Capabilities | None = None) -> ActionCapabilityMap:
    """Build a version-keyed Action capability map from the runtime probe (no disk I/O).

    Uses the supplied `capabilities` or runs a fresh probe. The map records the host's full Action
    surface keyed by the detected version string (or `UNKNOWN_VERSION`). Never assumes an Action
    exists — what the probe did not report is simply absent.
    """
    caps = capabilities if capabilities is not None else probe_capabilities()
    version = caps.inkscape_version or UNKNOWN_VERSION
    return ActionCapabilityMap(
        inkscape_version=version,
        inkscape_version_tuple=caps.inkscape_version_tuple,
        actions=list(caps.actions),
        action_count=len(caps.actions),
        probed_at=caps.probed_at,
    )


def persist_action_map(
    action_map: ActionCapabilityMap, settings: Settings | None = None
) -> Path | None:
    """Persist `action_map` under ``action-maps/<version>.json`` for the first workspace root.

    Returns the written path, or `None` when no workspace root is configured (persistence is
    best-effort — discovery still works without a root). The version key is charset-validated before
    path construction; the directory is created as a real dir.
    """
    s = settings if settings is not None else get_settings()
    if not s.workspace_roots:
        return None
    root = s.workspace_roots[0]
    version_key = _version_key(action_map.inkscape_version)
    sandbox.ensure_action_maps_dir(root)
    path = _map_path(root, version_key)
    path.write_text(action_map.model_dump_json(indent=2), encoding="utf-8")
    logger.info(
        "action map persisted",
        extra={
            "event": "file_io",
            "inkscape_version": action_map.inkscape_version,
            "action_count": action_map.action_count,
        },
    )
    return path


def load_action_map(version: str, settings: Settings | None = None) -> ActionCapabilityMap | None:
    """Load the persisted capability map for `version` from the first root, or `None` if absent.

    The version is reduced to a safe key first; an unknown/unsafe version yields `None` rather than
    raising. A corrupt on-disk map also degrades to `None`.
    """
    s = settings if settings is not None else get_settings()
    if not s.workspace_roots:
        return None
    root = s.workspace_roots[0]
    version_key = _version_key(version)
    try:
        path = _map_path(root, version_key)
    except KeyError:
        return None
    if not path.is_file():
        return None
    try:
        return ActionCapabilityMap.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def get_or_build_action_map(
    capabilities: Capabilities | None = None,
    settings: Settings | None = None,
) -> ActionCapabilityMap:
    """Return the persisted map for the detected version, building+persisting it on a miss.

    This is the consult-before-run entry point: the chain layer calls it to obtain the authoritative
    "Actions present on THIS Inkscape" set. On a cache miss (or no persisted root) it builds the map
    from the probe and persists it best-effort, so subsequent runs read it back from disk.
    """
    s = settings if settings is not None else get_settings()
    caps = capabilities if capabilities is not None else probe_capabilities()
    version = caps.inkscape_version or UNKNOWN_VERSION
    cached = load_action_map(version, settings=s)
    if cached is not None:
        return cached
    fresh = build_action_map(caps)
    persist_action_map(fresh, settings=s)
    return fresh


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
        # E10-10 S4: an empty allowlist is the default, not a probe failure — say so explicitly
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
