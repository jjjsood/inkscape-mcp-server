"""Headless `inkscape --shell` engine layer (ADR-007).

A warm, supervised, opt-in alternative transport for the Inkscape-engine ops (render / export /
path / boolean / action-chain): one long-lived `inkscape --shell` worker per document instead of a
fresh `inkscape …` process per call. Gated by `INKSCAPE_MCP_ENGINE_MODE=shell`; always carries an
automatic per-call CLI fallback so it can never regress correctness. A PRIVATE headless worker — NOT
a channel to the user's live GUI (architecture §4.4).
"""
