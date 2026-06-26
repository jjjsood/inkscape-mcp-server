# Changelog

All notable changes to `inkscape-mcp` are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - Unreleased

First public release. A Model Context Protocol (MCP) server that makes Inkscape / SVG documents
agent-ready over STDIO.

### Added
- **Headless document lifecycle** — `open_document` / `create_document` into tracked working copies
  keyed by an opaque `doc_id`; originals are never mutated.
- **Read & inspect** — `inspect_document`, `find_objects`, `validate_document`, `quality_report`
  (+ set variants), plus MCP resources exposing document structure and the runtime capability matrix.
- **Safe edits (medium risk, reversible)** — style (`set_fill` / `set_stroke` / `set_opacity` /
  `replace_color` / `apply_palette`), text/object, transforms, element creation, defs/gradients,
  grouping. Every mutation auto-snapshots and emits an Operation Record; `restore_snapshot` rolls back.
- **Typed batch & bulk** — `apply_edits` (ordered typed edits, validate-all-first, atomic, one
  snapshot) and `transform_objects` (selector → one typed op, `dry_run` default, `max_matches` cap).
- **Render & export** — `render_preview`, `export_document` / `export_object`, web / icon / print
  profiles, bounded dry-run-default batch export, with in-process content-truth verification.
- **High-risk surfaces (approval-gated)** — path geometry, Action chains, overwrite-on-save, delete,
  `set_document_svg` / `insert_svg_fragment`; each requires a per-operation approval token.
- **Live mode** — read / write / view-loop control of a running Inkscape via a cross-platform
  transport abstraction (extension-socket bridge on any OS; DBus fast-path on Linux), gated by
  `INKSCAPE_MCP_LIVE_ENABLED`.
- **Discoverability** — `how_do_i`, `list_capabilities`, generated `llms.txt` / `llms-full.txt`,
  MCP tool annotations + tags, and an opt-in `core` tool profile / `short` description mode to trim
  per-turn context cost.
- **Security model** — workspace sandbox with path + symlink guard, size / export / timeout limits,
  arg-list subprocess (never shell strings), safe XML parsing (no entity expansion), no network,
  no arbitrary extension execution, and risk-classed tools.

[0.1.0]: https://github.com/johnnyjagatpal/inkscape-mcp/releases/tag/v0.1.0
