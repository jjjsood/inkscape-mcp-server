# Contributing to inkscape-mcp

Thanks for your interest in improving `inkscape-mcp`. This document covers the local dev loop and
the conventions that keep the tool surface safe and predictable.

## Development setup

Requires **Python ≥ 3.12** and [`uv`](https://docs.astral.sh/uv/). The render / export / geometry
tools additionally need **Inkscape on `PATH`** (developed against 1.4.x); read / edit / validate
tools work without it.

```bash
uv sync                 # install runtime + dev dependencies
uv run inkscape-mcp     # start the STDIO MCP server
```

## Quality gates

All four must pass before a change is merged (CI enforces them on Linux/macOS/Windows):

```bash
uv run pytest                       # full suite
uv run pytest -m "not inkscape"     # skip tests needing the Inkscape binary
uv run ruff check --fix .           # lint (selects E,F,I,B,UP,S,RUF)
uv run ruff format .                # format
uv run mypy src                     # strict type check
```

Tests that need a real Inkscape binary are marked `@pytest.mark.inkscape` and auto-skip when no
`inkscape` is on `PATH`, so the suite stays green on a host without it.

## Tool conventions (non-negotiable)

The API is a surface of **small, strongly-typed tools** — never a portmanteau / `run_action(string)` /
`do_task(prompt)` free-text hatch. When adding or changing a tool:

- **One typed tool per capability**, with explicit parameters and type hints; the docstring is the
  tool description (keep the risk-class line — annotations are derived from it).
- **Declare a risk class** — `low` (read / render / export) · `medium` (write-new / style / text /
  transform; reversible) · `high` (overwrite / delete / path geometry / Action chains; approval-gated)
  · `restricted` (never ships).
- **Reversible by construction** — every mutating op runs through the edit pipeline: pre-mutation
  snapshot → apply → Operation Record. A genuine no-op writes nothing and reports `changed: false`.
- **Originals are sacred** — work on the working copy; saving goes to a new path; overwrites are
  approval-gated.
- **Subprocess via argument lists, never shell strings.** Safe XML parsing only (no entity expansion).
  Stay inside the workspace sandbox; no network; no arbitrary extension execution.

If your change adds, renames, or removes a tool / resource / prompt, regenerate the LLM index:

```bash
uv run python scripts/gen_llms_txt.py    # refresh llms.txt / llms-full.txt
```

## Security

Please report vulnerabilities privately — see [SECURITY.md](SECURITY.md). Do not open a public issue
for a security report.
