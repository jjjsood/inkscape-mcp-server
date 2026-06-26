<#
.SYNOPSIS
  Install the inkscape-mcp "Live Bridge" helper extension into Inkscape's user
  extensions directory on Windows (native PowerShell).

.DESCRIPTION
  Dynamic: resolves both the helper source and the target extensions dir at
  runtime. For Linux / macOS (or Windows under Git Bash / WSL) use
  install-live-helper.sh instead.

  Overrides (optional environment variables):
    HELPER_SRC_DIR        force the helper_extension source directory
    INKSCAPE_PROFILE_DIR  force the Inkscape profile directory

.EXAMPLE
  ./install-live-helper.ps1
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$helperName = 'inkscape_mcp_live'
$scriptDir  = $PSScriptRoot

# --- 1. locate the helper source (.py + .inx) -------------------------------
function Find-SrcDir {
    # explicit override wins
    if ($env:HELPER_SRC_DIR -and (Test-Path (Join-Path $env:HELPER_SRC_DIR "$helperName.inx"))) {
        return (Resolve-Path $env:HELPER_SRC_DIR).Path
    }
    # known package-relative path (scripts/ -> src/inkscape_mcp/live/helper_extension)
    $rel = Join-Path $scriptDir '..\src\inkscape_mcp\live\helper_extension'
    if (Test-Path (Join-Path $rel "$helperName.inx")) {
        return (Resolve-Path $rel).Path
    }
    # last resort: search upward one level then downward for the .inx
    $hit = Get-ChildItem -Path (Join-Path $scriptDir '..') -Recurse -Filter "$helperName.inx" `
        -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($hit) { return $hit.Directory.FullName }
    return $null
}

$srcDir = Find-SrcDir
if (-not $srcDir) {
    Write-Error "helper source ($helperName.inx/.py) not found. Set HELPER_SRC_DIR to the helper_extension directory."
}

foreach ($f in @("$helperName.py", "$helperName.inx")) {
    if (-not (Test-Path (Join-Path $srcDir $f))) {
        Write-Error "missing source file $(Join-Path $srcDir $f)"
    }
}

# --- 2. locate the Inkscape user extensions dir -----------------------------
$extDir = $null
if ($env:INKSCAPE_PROFILE_DIR) {
    $extDir = Join-Path $env:INKSCAPE_PROFILE_DIR 'extensions'
}
elseif (Get-Command inkscape -ErrorAction SilentlyContinue) {
    # Inkscape 1.x prints the per-user profile dir and exits; correct on every OS.
    # (Requires the console build, inkscape.com, to be the one on PATH.)
    try {
        $udir = (& inkscape --user-data-directory 2>$null | Select-Object -First 1)
        if ($udir) { $extDir = Join-Path ($udir.Trim()) 'extensions' }
    } catch { }
}
# fallback: Windows Inkscape uses %APPDATA%\inkscape
if (-not $extDir) {
    $base = if ($env:APPDATA) { $env:APPDATA } else { Join-Path $HOME 'AppData\Roaming' }
    $extDir = Join-Path $base 'inkscape\extensions'
}

# --- 3. install -------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $extDir | Out-Null
foreach ($f in @("$helperName.py", "$helperName.inx")) {
    Copy-Item -Force -Path (Join-Path $srcDir $f) -Destination $extDir
}

Write-Host "Installed inkscape-mcp Live Bridge helper:"
Write-Host "  from: $srcDir"
Write-Host "  to:   $extDir"
Get-ChildItem -Path (Join-Path $extDir "$helperName.py"), (Join-Path $extDir "$helperName.inx") |
    Format-Table Length, FullName -AutoSize

Write-Host @"

Next steps:
  1. Enable the gate: add  "INKSCAPE_MCP_LIVE_ENABLED": "1"  to the inkscape-mcp
     env block in .mcp.json, then restart the MCP session (/mcp reconnect).
  2. Start (or restart) the Inkscape GUI - extensions load only at startup.
  3. In Inkscape run:  Extensions -> inkscape-mcp -> inkscape-mcp Live Bridge
     (this opens the loopback socket the server connects to).
  4. Call live_connect, then live_status / check_live_support to confirm.
"@
