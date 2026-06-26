# Security Policy

## Reporting a vulnerability

Please report security issues **privately**. Do not open a public GitHub issue for a vulnerability.

Use GitHub's private vulnerability reporting on the repository
([Security → Report a vulnerability](https://github.com/johnnyjagatpal/inkscape-mcp/security/advisories/new)),
or open a minimal-detail tracking issue at
<https://github.com/johnnyjagatpal/inkscape-mcp/issues> asking for a private channel.

Please include a description, reproduction steps, and the affected version. You will get an
acknowledgement and a fix or mitigation plan as soon as practical.

## Security model

`inkscape-mcp` is designed to let an autonomous agent mutate real files safely. Every tool runs
inside a defense-in-depth model:

- **Workspace sandbox** — the server only reads / writes inside the configured
  `INKSCAPE_MCP_WORKSPACE_ROOTS`; everything else is rejected.
- **Path normalization + symlink guard** — relative paths anchor to the workspace root (never the
  process CWD); a path escaping the sandbox (including via symlink) is refused with
  `path rejected: outside workspace`. No absolute host path ever appears in a tool result.
- **Originals are never mutated** — documents open into tracked working copies; saving writes to a
  new file, and overwriting an existing file requires explicit `overwrite=True` **and** an approval
  token.
- **Risk classes + approval gate** — each tool declares `low` / `medium` / `high` / `restricted`.
  High-risk operations (overwrite / delete / path geometry / Action chains / raw Action) refuse
  without a non-empty per-operation `approval_token`; restricted tools never ship.
- **Reversibility** — every mutation is snapshot-backed and recorded as an Operation Record;
  `restore_snapshot` rolls back.
- **Argument-list subprocess** — the Inkscape CLI is always invoked with an argv list, never a shell
  string (`shell=False`).
- **Safe XML parsing** — entity expansion is disabled (no XXE / billion-laughs); composed-SVG entry
  points (`set_document_svg` / `insert_svg_fragment`) apply a strict element / attribute allowlist
  that rejects `<script>`, `on*` handlers, and `javascript:` / external / `data:` hrefs.
- **No network, no arbitrary code** — the server makes no outbound network calls and exposes no
  free-text code / raw-Action passthrough; Inkscape Actions and extensions run only from a
  server-side allowlist that a client cannot widen.
- **Resource limits** — input / output / export size caps, per-process timeout, and a bounded number
  of concurrent Inkscape subprocesses.

## Supported versions

This project is pre-1.0; security fixes target the latest released version.
