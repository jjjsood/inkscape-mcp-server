#!/usr/bin/env python3
"""Cross-platform STDIO boot smoke for the ``inkscape-mcp`` entry point (E7-02 / E7-01).

Launches the installed console script (or any command passed as argv), immediately closes its
stdin (EOF), and asserts it shuts down cleanly within a timeout — proving the package imports
and the FastMCP STDIO server starts on this OS without an import error. Works identically on
Linux, macOS, and Windows (no shell redirection tricks, no `/dev/null`).

Usage:

    # Smoke the console script that pipx/uvx put on PATH:
    python scripts/ci_boot_smoke.py inkscape-mcp

    # Or smoke a uvx-launched build straight from the source tree:
    python scripts/ci_boot_smoke.py uvx --from . inkscape-mcp

Exit code 0 = booted and exited cleanly on EOF (or was still happily running at the timeout,
which is also a pass: an MCP server legitimately blocks on stdin). Non-zero = the process died
with an error before/at EOF (e.g. ImportError, bad entry point).
"""

from __future__ import annotations

import subprocess
import sys

#: Generous so a cold uvx/pipx environment has time to import; the server boots far faster.
_TIMEOUT_S = 60.0


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: ci_boot_smoke.py <command> [args...]", file=sys.stderr)
        return 2

    print(f"boot smoke: launching {argv!r} and feeding EOF", flush=True)
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # Close stdin (EOF). A well-behaved STDIO MCP server either exits cleanly or keeps waiting;
    # either is fine. A broken entry point dies quickly with a non-zero code + a traceback.
    try:
        out, _ = proc.communicate(input="", timeout=_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # Still running after EOF => it booted and is blocking on the transport. That is a PASS.
        proc.kill()
        out, _ = proc.communicate()
        if out:
            print(out, flush=True)
        print("boot smoke: server still running at timeout (booted OK, killed)", flush=True)
        return 0

    if out:
        print(out, flush=True)

    if proc.returncode == 0:
        print("boot smoke: clean exit on EOF (returncode 0)", flush=True)
        return 0

    print(f"boot smoke FAILED: process exited {proc.returncode}", file=sys.stderr, flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
