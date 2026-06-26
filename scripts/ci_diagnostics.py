#!/usr/bin/env python3
"""Print the runtime capability matrix as JSON (CI diagnostics, E7-02).

Runs the same `probe_capabilities()` the `diagnose_runtime` MCP tool uses, so a CI failure
caused by a missing dependency (no Inkscape binary, no fontconfig, no session bus, ...) is
legible in the job log instead of opaque. Pure-function probe — needs no MCP host and degrades
gracefully (absent backends become null/false fields plus a `notes` entry, never an exception).

Usage (cross-platform, run inside ``inkscape-mcp-server/``):

    uv run python scripts/ci_diagnostics.py
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    from inkscape_mcp.runtime.probe import probe_capabilities

    caps = probe_capabilities()
    print(json.dumps(caps.model_dump(), indent=2, default=str))
    # A green probe is informational only; the matrix is the point. Always exit 0 so the
    # diagnostics step itself never fails the pipeline — the real gates are ruff/mypy/pytest.
    return 0


if __name__ == "__main__":
    sys.exit(main())
