"""Live-mode layer (E3 — Phase 3, Live read).

Detect and read a *running* Inkscape instance cross-platform through a transport abstraction
(architecture §4.4/§4.5). The portable backend is an extension-socket bridge (a shipped
fixed-purpose `inkex` helper on a loopback socket); on Linux a DBus `org.gtk.Actions` backend is
an optional fast-path. The live gate is ON by default (X1, operator decision 2026-06-14; set the
env falsy to opt out) and nothing here is ever required for headless.

Security (sec.12 / restricted risk class): loopback-only sockets, a token handshake before any
command, a fixed semantic command schema (NO code / raw-Action passthrough — ADR-003), arg-list
subprocesses only, and writes that go through the workspace policy layer so a live fault can
never damage workspace files.
"""

from __future__ import annotations
