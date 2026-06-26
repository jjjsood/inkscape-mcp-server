# Install — `uvx` / `pipx` / from source

`inkscape-mcp` is a standard Python package (PEP 621 `pyproject.toml`, `hatchling` build backend)
that exposes a single console script:

```toml
[project.scripts]
inkscape-mcp = "inkscape_mcp.server:main"
```

`inkscape_mcp.server:main` starts the FastMCP app over **STDIO**. That entry point is what every MCP
host launches.

> **Not on PyPI (yet).** The package is not published, so `uvx inkscape-mcp` /
> `pipx install inkscape-mcp` by bare name will not resolve. Use the **from-source / from-git** forms
> below — they install the exact same package and console script. Once published, the bare-name forms
> in the last section will work unchanged.

## Requirements

- **Python ≥ 3.12** (`requires-python = ">=3.12"`).
- **Inkscape 1.4.x on `PATH`** for render / export / path-geometry / live tools. Read / edit /
  validate / snapshot tools work **without** Inkscape. (Minimum detected version floor is 1.3.0; the
  server **detects at runtime and never assumes** a version — run `diagnose_runtime`.)
- At least one **workspace root** (`INKSCAPE_MCP_WORKSPACE_ROOTS`) — the sandbox refuses to touch
  anything outside it. See [host-configs.md](host-configs.md) for all env vars.

## `uvx` (run without installing)

`uvx` builds an ephemeral environment and runs the console script. Best for trying the server or for
host configs that prefer a zero-install launch command.

```bash
# From a local checkout (the repo root holds pyproject.toml):
uvx --from /abs/path/to/inkscape-mcp inkscape-mcp

# Straight from git:
uvx --from "git+https://github.com/johnnyjagatpal/inkscape-mcp.git" inkscape-mcp
```

The launched process waits on stdin for MCP JSON-RPC — that is correct; an MCP host drives it. To
just confirm it boots, send EOF (Ctrl-D) or pipe `</dev/null` and check there is no import error.

## `pipx` (persistent install)

`pipx` installs the console script into an isolated environment on your `PATH`.

```bash
# From a local checkout:
pipx install /abs/path/to/inkscape-mcp

# From git:
pipx install "git+https://github.com/johnnyjagatpal/inkscape-mcp.git"

# Then:
inkscape-mcp           # starts the STDIO server (waits on stdin)
pipx upgrade inkscape-mcp
pipx uninstall inkscape-mcp
```

## From source with `uv` (development / dogfooding)

```bash
cd inkscape-mcp           # the repo root (holds pyproject.toml)
uv sync                   # install runtime + dev dependencies into .venv
uv run inkscape-mcp       # start the STDIO server
uv run pytest             # run the test suite
```

A host can launch this checkout directly with `uv run --directory /abs/path/to/inkscape-mcp
inkscape-mcp` for dogfooding once `uv sync` has run.

## Verify the entry point boots

Any of these confirm the package + console script are wired correctly:

```bash
# Import check (no stdin wait):
uv run python -c "from inkscape_mcp.server import main; print('ok')"

# Boot over STDIO and exit cleanly on EOF:
uv run inkscape-mcp </dev/null

# Build the distributions (proves it is uvx/pipx-installable):
uv build        # -> dist/inkscape_mcp-<ver>.tar.gz + .whl
```

## When published to PyPI (future)

Once the package is on PyPI, the bare-name forms work with no extra flags:

```bash
uvx inkscape-mcp
pipx install inkscape-mcp
```

Nothing else changes — same console script, same STDIO transport, same env vars.
