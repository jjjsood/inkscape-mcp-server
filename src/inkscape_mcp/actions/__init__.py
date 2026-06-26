"""Controlled Inkscape Action/Extension surface.

Pure logic only — no MCP decorators. Three concerns:

- :mod:`inkscape_mcp.actions.capability_map` — discover the host's actual Action surface from the
  runtime probe and persist a version-keyed capability map under the workspace ``.inkscape-mcp/``
  area, so execution can consult "does this Action exist on THIS Inkscape?" and degrade when not.
- :mod:`inkscape_mcp.actions.chains` — validate an ordered list of typed Action steps against the
  server-side allowlist AND the version map, assemble the ``--actions`` argv (arg-lists only,
  ``shell=False``), and execute via the Inkscape engine. Discovery is low risk; execution is HIGH
  risk + approval-gated and refused for anything not allowlisted-and-present.

This is the gate machinery (the raw-action escape hatch) builds on — there is no open-string
Action passthrough here (ADR-003; project rule: no arbitrary extension exec; sec.12).
"""
