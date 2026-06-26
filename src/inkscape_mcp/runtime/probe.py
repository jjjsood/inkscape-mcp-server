"""Runtime capability probe engine.

Runtime detection notes for. Every probe runs via
the foundation subprocess wrapper (arg lists only, bounded timeout) and DEGRADES GRACEFULLY:
a missing ``inkscape`` / ``inkex`` / ``gdbus`` / ``fc-list`` yields null/false fields plus a
note, never an exception. **Detect at runtime; never assume 1.4.3.**

Pure functions only — no MCP decorators. The tools/resources layer wraps `probe_capabilities`.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from inkscape_mcp.config import Settings, get_settings
from inkscape_mcp.intents import IntentMatch, intents_summary
from inkscape_mcp.logging_setup import get_logger
from inkscape_mcp.workspace.subprocess_exec import ProcessError, run_process

logger = get_logger("runtime.probe")

#: Minimum Inkscape version this server targets. Below this, `meets_minimum` is False.
MINIMUM_VERSION: tuple[int, int, int] = (1, 3, 0)

# Matches "Inkscape 1.4.3 (0d15f75042, 2025-12-25)" -> (1, 4, 3).
_VERSION_RE = re.compile(r"Inkscape\s+(\d+)\.(\d+)(?:\.(\d+))?")

# Matches the bracketed token list after --export-type in `inkscape --help`, e.g.
# "--export-type=TYPE  File type(s) to export: [svg,png,ps,eps,pdf,emf,wmf,xaml]".
_EXPORT_TYPE_RE = re.compile(r"--export-type\S*.*?\[([^\]]+)\]", re.DOTALL)


class ToolInfo(BaseModel):
    """One entry in the authoritative `Capabilities.tools` list.

    Sourced from the LIVE FastMCP registry (`mcp.list_tools()`), so the index can never describe a
    tool that is not registered or miss one that is. Carries the tool's name plus its one-line
    purpose (first docstring line) and risk class (parsed from the `Risk class:` docstring line —
    same derivation `scripts/gen_llms_txt.py` uses for `llms.txt`).
    """

    name: str = Field(description="Registered MCP tool name (the `@mcp.tool` callable).")
    purpose: str = Field(description="One-line purpose — the first non-empty docstring line.")
    risk: str = Field(
        description="Risk class parsed from the tool docstring (low/medium/high/restricted)."
    )


class Capabilities(BaseModel):
    """Machine-readable snapshot of the local Inkscape + Python runtime.

    Produced by `probe_capabilities`. Every field is detected at runtime; absent backends
    surface as null/false plus an entry in `notes`, not an exception. Becomes the
    `outputSchema` of the `diagnose_runtime` / `list_capabilities` tools and the
    `inkscape://runtime/capabilities` resource.

    The `tool_count` + `tools` fields are NOT probed from Inkscape: they are the
    authoritative MCP tool surface, populated from the live FastMCP registry by the
    `inkscape_mcp.tools.system` layer (`with_registry_tools`) before the matrix is served, so the
    one count agrees with `mcp.list_tools()` and `llms.txt` and agents stop deriving it four ways.
    """

    inkscape_available: bool = Field(description="Whether an Inkscape binary was found and ran.")
    inkscape_binary: str | None = Field(
        default=None, description="Absolute path to the Inkscape binary, or null if absent."
    )
    inkscape_version: str | None = Field(
        default=None, description="Raw version string reported by `inkscape --version`."
    )
    inkscape_version_tuple: tuple[int, int, int] | None = Field(
        default=None, description="Parsed (major, minor, patch) version, or null if unparsable."
    )
    meets_minimum: bool = Field(
        default=False,
        description=f"Whether the detected version is >= MINIMUM_VERSION {MINIMUM_VERSION}.",
    )

    actions: list[str] = Field(
        default_factory=list,
        description="Action ids from `inkscape --action-list` (enumeration only; not executable).",
    )
    has_export_actions: bool = Field(
        default=False, description="Whether any `export-*` action is present."
    )
    has_object_actions: bool = Field(
        default=False, description="Whether any `object-*` action is present."
    )
    has_path_actions: bool = Field(
        default=False, description="Whether any `path-*` action is present."
    )
    has_select_actions: bool = Field(
        default=False, description="Whether any `select*` action is present."
    )

    export_types: list[str] = Field(
        default_factory=list,
        description="Export-type tokens parsed from the `--export-type=` list in inkscape --help.",
    )

    shell_mode_available: bool = Field(
        default=False,
        description=(
            "Whether `inkscape --shell` (the headless shell engine/ADR-007) can run here. "
            "True when an Inkscape binary is present (shell mode ships on all supported 1.x). "
            "Whether the warm engine is USED is the separate INKSCAPE_MCP_ENGINE_MODE gate."
        ),
    )

    system_data_dir: str | None = Field(
        default=None, description="Inkscape system data directory (`--system-data-directory`)."
    )
    user_data_dir: str | None = Field(
        default=None, description="Inkscape user data directory (`--user-data-directory`)."
    )

    python_version: str = Field(description="Interpreter version running this server.")
    inkex_path: str | None = Field(
        default=None, description="Path to bundled `inkex/__init__.py`, or null if not found."
    )
    inkex_version: str | None = Field(
        default=None,
        description="`__version__` read from inkex sources (NOT imported), or null if unknown.",
    )

    dbus_session_bus: bool = Field(
        default=False, description="Whether DBUS_SESSION_BUS_ADDRESS is set (session bus present)."
    )
    dbus_inkscape_present: bool = Field(
        default=False,
        description="Whether an `org.inkscape.Inkscape*` name is on the session bus right now.",
    )
    live_extension_socket_available: bool = Field(
        default=False,
        description="Whether the live helper extension is installed under a data dir (unshipped).",
    )

    font_count: int = Field(
        default=0, description="Font faces reported by `fc-list` (0 ⇒ fontconfig broken/absent)."
    )

    probed_at: str = Field(description="UTC ISO-8601 timestamp of when this probe ran.")
    notes: list[str] = Field(
        default_factory=list,
        description="Human-readable degradation messages (e.g. 'inkscape not found').",
    )
    intents: list[IntentMatch] = Field(
        default_factory=intents_summary,
        description=(
            "Curated natural-language goal → tool(s) map: each entry is "
            "{goal_pattern, tools, how_to, group}. The same data the read-only `how_do_i` tool "
            "matches against; surfaced here so an agent can browse the whole map. Guidance only — "
            "executes nothing, no raw-action hatch (ADR-003). Host-independent (not probed)."
        ),
    )

    tool_count: int = Field(
        default=0,
        description=(
            "Authoritative number of registered `@mcp.tool`s. Equals `len(tools)` and the "
            "live `mcp.list_tools()` count — one unambiguous number so agents stop deriving it. "
            "Populated from the live FastMCP registry, not probed from Inkscape."
        ),
    )
    tools: list[ToolInfo] = Field(
        default_factory=list,
        description=(
            "The authoritative MCP tool surface: each entry is {name, purpose, risk}, "
            "sourced from the live FastMCP registry. Counts only `@mcp.tool`s (not resources or "
            "prompts). Populated by the tools/system layer before serving; empty on a raw probe."
        ),
    )


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_version(text: str) -> tuple[int, int, int] | None:
    """Parse an Inkscape version string into a (major, minor, patch) tuple, or None."""
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3)) if match.group(3) is not None else 0
    return (major, minor, patch)


def _parse_export_types(help_text: str) -> list[str]:
    """Parse the bracketed `--export-type=` token list out of `inkscape --help`."""
    match = _EXPORT_TYPE_RE.search(help_text)
    if match is None:
        return []
    raw = match.group(1)
    tokens = [tok.strip() for tok in re.split(r"[,\s]+", raw) if tok.strip()]
    # De-duplicate while preserving order.
    seen: set[str] = set()
    result: list[str] = []
    for tok in tokens:
        if tok not in seen:
            seen.add(tok)
            result.append(tok)
    return result


def _parse_action_list(stdout: str) -> list[str]:
    """Parse `inkscape --action-list` output to action ids.

    Each line is ``action-id : localized description``; the id is the first token before the
    first colon (action ids use dots, never bare colons). Blank/garbled lines are skipped.
    """
    actions: list[str] = []
    seen: set[str] = set()
    for line in stdout.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        action_id = line.split(":", 1)[0].strip()
        if not action_id or action_id in seen:
            continue
        seen.add(action_id)
        actions.append(action_id)
    return actions


def _probe_inkscape_cli(args: list[str], timeout_s: float, notes: list[str]) -> str | None:
    """Run `inkscape <args>` directly via the resolved binary; return stdout or None.

    Resolves the binary once via `shutil.which` so a missing Inkscape degrades to a note
    instead of raising. Non-zero exit / timeout also degrade to None + a note.
    """
    binary = shutil.which("inkscape")
    if binary is None:
        return None
    try:
        result = run_process([binary, *args], timeout_s=timeout_s)
    except ProcessError as exc:
        notes.append(f"inkscape {' '.join(args)} failed to launch: {exc}")
        return None
    if result.timed_out:
        notes.append(f"inkscape {' '.join(args)} timed out after {timeout_s}s")
        return None
    if result.returncode != 0:
        notes.append(f"inkscape {' '.join(args)} exited {result.returncode}")
        return None
    return result.stdout


def _probe_inkex(data_dirs: list[str], notes: list[str]) -> tuple[str | None, str | None]:
    """Locate `inkex/__init__.py` under the data dirs and read `__version__` from its text.

    NEVER imports inkex (that triggers a numpy import chain which fails on this host). Returns
    (inkex_path, inkex_version); either may be None with a note recorded.
    """
    init_path: Path | None = None
    for data_dir in data_dirs:
        candidate = Path(data_dir) / "extensions" / "inkex" / "__init__.py"
        try:
            if candidate.is_file():
                init_path = candidate
                break
        except OSError:
            continue
    if init_path is None:
        notes.append("inkex not found under any data dir")
        return None, None

    try:
        text = init_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        notes.append(f"inkex __init__.py unreadable: {exc}")
        return str(init_path), None

    match = re.search(r"""__version__\s*=\s*['"]([^'"]+)['"]""", text)
    if match is None:
        notes.append("inkex __version__ not found in sources")
        return str(init_path), None
    return str(init_path), match.group(1)


def _probe_dbus_inkscape(timeout_s: float, notes: list[str]) -> bool:
    """Whether a live `org.inkscape.Inkscape` instance answers on the session bus via `gdbus`.

    Probes `org.gtk.Actions.List` reachability rather than `gdbus list-names`: Inkscape 1.4.x does
    NOT publish its (running) well-known name in `list-names`, and the name has no DBus `.service`
    file, so a List call against a non-running instance fails cleanly with `ServiceUnknown` and
    never spawns Inkscape (mirrors `live.dbus_backend.DBusTransport._actions_list_reachable`).
    Tolerates `gdbus` missing (records a note, returns False). Never raises.
    """
    binary = shutil.which("gdbus")
    if binary is None:
        notes.append("gdbus unavailable; cannot probe live Inkscape on session bus")
        return False
    try:
        result = run_process(
            [
                binary,
                "call",
                "--session",
                "--dest",
                "org.inkscape.Inkscape",
                "--object-path",
                "/org/inkscape/Inkscape",
                "--method",
                "org.gtk.Actions.List",
            ],
            timeout_s=timeout_s,
        )
    except ProcessError as exc:
        notes.append(f"gdbus failed to launch: {exc}")
        return False
    if result.timed_out:
        notes.append(f"gdbus org.gtk.Actions.List timed out after {timeout_s}s")
        return False
    # Non-zero is the normal "no live instance" case (ServiceUnknown) — not an error to surface.
    return result.returncode == 0


def _probe_live_extension(data_dirs: list[str]) -> bool:
    """Whether the live helper extension is installed under a data dir's `extensions/`.

    The helper is not shipped yet, so this is expected False. Detection is by presence of a
    marker file under `extensions/`. Never raises.
    """
    for data_dir in data_dirs:
        marker = Path(data_dir) / "extensions" / "inkscape_mcp_live.py"
        try:
            if marker.is_file():
                return True
        except OSError:
            continue
    return False


def _probe_font_count(timeout_s: float, notes: list[str]) -> int:
    """Count font faces via `fc-list`; 0 ⇒ fontconfig broken/absent. Never raises."""
    binary = shutil.which("fc-list")
    if binary is None:
        notes.append("fc-list unavailable; font count is 0")
        return 0
    try:
        result = run_process([binary], timeout_s=timeout_s)
    except ProcessError as exc:
        notes.append(f"fc-list failed to launch: {exc}")
        return 0
    if result.timed_out:
        notes.append(f"fc-list timed out after {timeout_s}s")
        return 0
    if result.returncode != 0:
        notes.append(f"fc-list exited {result.returncode}")
        return 0
    return sum(1 for line in result.stdout.splitlines() if line.strip())


def probe_capabilities(settings: Settings | None = None) -> Capabilities:
    """Probe the local runtime and return a machine-readable `Capabilities` snapshot.

    Runs the capability-matrix probe recipe via `run_process` with a bounded per-subprocess
    timeout (`settings.process_timeout_s`). Every probe degrades gracefully — absent backends
    become null/false fields plus a `notes` entry, never an exception. Detects at runtime;
    never assumes a specific Inkscape version.
    """
    s = settings if settings is not None else get_settings()
    timeout_s = s.process_timeout_s
    notes: list[str] = []

    python_version = platform.python_version()

    # --- Inkscape binary + version ---
    inkscape_binary = shutil.which("inkscape")
    inkscape_available = inkscape_binary is not None
    inkscape_version: str | None = None
    inkscape_version_tuple: tuple[int, int, int] | None = None
    meets_minimum = False

    if not inkscape_available:
        notes.append("inkscape not found on PATH")
    else:
        version_out = _probe_inkscape_cli(["--version"], timeout_s, notes)
        if version_out is not None:
            inkscape_version = version_out.strip() or None
            inkscape_version_tuple = _parse_version(version_out)
            if inkscape_version_tuple is None:
                notes.append("could not parse inkscape version string")
            else:
                meets_minimum = inkscape_version_tuple >= MINIMUM_VERSION

    # --- Actions ---
    actions: list[str] = []
    if inkscape_available:
        action_out = _probe_inkscape_cli(["--action-list"], timeout_s, notes)
        if action_out is not None:
            actions = _parse_action_list(action_out)
            if not actions:
                notes.append("inkscape --action-list returned no parseable actions")

    has_export_actions = any(a.startswith("export-") for a in actions)
    has_object_actions = any(a.startswith("object-") for a in actions)
    has_path_actions = any(a.startswith("path-") for a in actions)
    has_select_actions = any(a.startswith("select") for a in actions)

    # --- Export types (from --help) ---
    export_types: list[str] = []
    if inkscape_available:
        help_out = _probe_inkscape_cli(["--help"], timeout_s, notes)
        if help_out is not None:
            export_types = _parse_export_types(help_out)
            if not export_types:
                notes.append("could not parse --export-type list from inkscape --help")

    # --- Data dirs ---
    system_data_dir: str | None = None
    user_data_dir: str | None = None
    if inkscape_available:
        sys_out = _probe_inkscape_cli(["--system-data-directory"], timeout_s, notes)
        if sys_out is not None:
            system_data_dir = sys_out.strip() or None
        user_out = _probe_inkscape_cli(["--user-data-directory"], timeout_s, notes)
        if user_out is not None:
            user_data_dir = user_out.strip() or None

    data_dirs = [d for d in (system_data_dir, user_data_dir) if d]

    # --- inkex (read sources, never import) ---
    inkex_path, inkex_version = _probe_inkex(data_dirs, notes)

    # --- DBus / live transport ---
    dbus_session_bus = bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS", "").strip())
    dbus_inkscape_present = _probe_dbus_inkscape(timeout_s, notes) if dbus_session_bus else False
    if not dbus_session_bus:
        notes.append("no DBUS_SESSION_BUS_ADDRESS; session bus unavailable")
    live_extension_socket_available = _probe_live_extension(data_dirs)

    # --- Fonts ---
    font_count = _probe_font_count(timeout_s, notes)
    if font_count == 0 and "fc-list unavailable; font count is 0" not in notes:
        notes.append("font count is 0; fontconfig may be broken")

    capabilities = Capabilities(
        inkscape_available=inkscape_available,
        inkscape_binary=inkscape_binary,
        inkscape_version=inkscape_version,
        inkscape_version_tuple=inkscape_version_tuple,
        meets_minimum=meets_minimum,
        actions=actions,
        has_export_actions=has_export_actions,
        has_object_actions=has_object_actions,
        has_path_actions=has_path_actions,
        has_select_actions=has_select_actions,
        export_types=export_types,
        shell_mode_available=inkscape_available,
        system_data_dir=system_data_dir,
        user_data_dir=user_data_dir,
        python_version=python_version,
        inkex_path=inkex_path,
        inkex_version=inkex_version,
        dbus_session_bus=dbus_session_bus,
        dbus_inkscape_present=dbus_inkscape_present,
        live_extension_socket_available=live_extension_socket_available,
        font_count=font_count,
        probed_at=_utc_now_iso(),
        notes=notes,
    )
    logger.info(
        "runtime probed",
        extra={
            "event": "process_exec",
            "inkscape_available": inkscape_available,
            "inkscape_version": inkscape_version,
            "meets_minimum": meets_minimum,
            "action_count": len(actions),
            "export_types": export_types,
            "font_count": font_count,
            "note_count": len(notes),
        },
    )
    return capabilities
