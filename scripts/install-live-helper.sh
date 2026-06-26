#!/usr/bin/env bash
# Install the inkscape-mcp "Live Bridge" helper extension into Inkscape's user
# extensions directory (Linux / macOS / Windows-under-Git-Bash-or-WSL).
#
# Dynamic: resolves both the helper source and the target extensions dir at
# runtime, so it works regardless of where the repo lives or how Inkscape is
# configured. For native Windows (PowerShell) use install-live-helper.ps1.
#
# Overrides (optional):
#   HELPER_SRC_DIR=/path/to/helper_extension   force the source directory
#   INKSCAPE_PROFILE_DIR=/path/to/profile       force the Inkscape profile dir
#
# Usage: ./install-live-helper.sh
set -euo pipefail

helper_name="inkscape_mcp_live"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- 1. locate the helper source (.py + .inx) -------------------------------
find_src() {
  # explicit override wins
  if [[ -n "${HELPER_SRC_DIR:-}" && -f "$HELPER_SRC_DIR/${helper_name}.inx" ]]; then
    printf '%s\n' "$HELPER_SRC_DIR"; return 0
  fi
  # known package-relative path (scripts/ -> src/inkscape_mcp/live/helper_extension)
  local rel="$script_dir/../src/inkscape_mcp/live/helper_extension"
  if [[ -f "$rel/${helper_name}.inx" ]]; then
    (cd "$rel" && pwd); return 0
  fi
  # last resort: search upward one level then downward for the .inx
  local hit
  hit="$(find "$script_dir/.." -type f -name "${helper_name}.inx" -print -quit 2>/dev/null || true)"
  if [[ -n "$hit" ]]; then dirname "$hit"; return 0; fi
  return 1
}

src_dir="$(find_src)" || {
  echo "ERROR: helper source (${helper_name}.inx/.py) not found." >&2
  echo "       Set HELPER_SRC_DIR to the helper_extension directory." >&2
  exit 1
}

for f in "${helper_name}.py" "${helper_name}.inx"; do
  [[ -f "$src_dir/$f" ]] || { echo "ERROR: missing source file $src_dir/$f" >&2; exit 1; }
done

# --- 2. locate the Inkscape user extensions dir -----------------------------
ext_dir=""
if [[ -n "${INKSCAPE_PROFILE_DIR:-}" ]]; then
  ext_dir="$INKSCAPE_PROFILE_DIR/extensions"
elif command -v inkscape >/dev/null 2>&1; then
  # Inkscape 1.x prints the per-user profile dir and exits; correct on every OS.
  udir="$(inkscape --user-data-directory 2>/dev/null | tr -d '\r' | head -n1 || true)"
  [[ -n "$udir" ]] && ext_dir="$udir/extensions"
fi
# OS-aware fallback if Inkscape could not be probed
if [[ -z "$ext_dir" ]]; then
  case "${OSTYPE:-$(uname -s 2>/dev/null || echo unknown)}" in
    msys*|cygwin*|win32*|MINGW*|MSYS*)
      # Git Bash / Cygwin / MSYS on Windows: Inkscape uses %APPDATA%\inkscape
      base="${APPDATA:-$HOME/AppData/Roaming}"
      ext_dir="$base/inkscape/extensions" ;;
    *)
      # Linux and macOS (Inkscape 1.x uses XDG-style ~/.config/inkscape)
      ext_dir="${XDG_CONFIG_HOME:-$HOME/.config}/inkscape/extensions" ;;
  esac
fi

# --- 3. install -------------------------------------------------------------
mkdir -p "$ext_dir"
cp -f "$src_dir/${helper_name}.py" "$src_dir/${helper_name}.inx" "$ext_dir/"

echo "Installed inkscape-mcp Live Bridge helper:"
echo "  from: $src_dir"
echo "  to:   $ext_dir"
ls -l "$ext_dir/${helper_name}.py" "$ext_dir/${helper_name}.inx"

cat <<'NEXT'

Next steps:
  1. The live gate is ON by default (operator decision 2026-06-14). To opt OUT,
     set  "INKSCAPE_MCP_LIVE_ENABLED": "0"  in the inkscape-mcp env block in
     .mcp.json, then restart the MCP session (/mcp reconnect).
  2. Start (or restart) the Inkscape GUI — extensions load only at startup.
  3. In Inkscape run:  Extensions -> inkscape-mcp -> inkscape-mcp Live Bridge
     (this opens the loopback socket the server connects to).
  4. Call live_connect, then live_status / check_live_support to confirm.
NEXT
