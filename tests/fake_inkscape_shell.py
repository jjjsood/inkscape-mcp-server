"""A fake `inkscape --shell` for engine tests (E12) — no real Inkscape required.

Mirrors the real shell's framing exactly (the E12-02 spike findings): a startup banner ending in the
bare ``"> "`` prompt, each command ECHOED back on its own line, any output lines, then a fresh
``"> "`` prompt with no trailing newline. Unknown actions print the real
``InkscapeApplication::parse_actions: could not find action for: <X>`` to STDERR. Export options
are STICKY across commands (as on the real shell). This lets the production framing / threading /
timeout / lifecycle / routing code run end-to-end on CI hosts with no Inkscape.

Test hooks (never real action names): ``__sleep__:<secs>`` blocks (to trigger a timeout);
``__crash__`` exits abruptly (to exercise crash-restart). Run via ``[sys.executable, this_file]``.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_PROMPT = "> "


def _write_png(path: Path, width: int, height: int) -> None:
    from PIL import Image

    Image.new("RGB", (max(1, width), max(1, height)), (200, 200, 200)).save(path)


_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 60" width="100" height="60">'
    '<rect id="r1" x="10" y="10" width="30" height="20" fill="#36c"/></svg>'
)


def main() -> int:
    sys.stdout.write(
        "Inkscape interactive shell mode. Type 'action-list' to list all actions.\n" + _PROMPT
    )
    sys.stdout.flush()

    export_filename: str | None = None
    export_type = "png"
    export_width = 0
    opened = "100 60"  # tracks intrinsic size of the open doc (cosmetic)

    for raw in sys.stdin:
        cmd = raw.rstrip("\n")
        # The real shell echoes the whole input line back first.
        echoed: list[str] = []

        if cmd == "quit":
            sys.stdout.write(cmd + "\n")
            sys.stdout.flush()
            return 0
        if cmd == "__crash__":
            return 7
        # A single input line may carry several ``;``-joined actions (the shell's grammar).
        actions = [a.strip() for a in cmd.split(";")]
        for action in actions:
            verb, _, arg = action.partition(":")
            verb = verb.strip()
            arg = arg.strip()
            if verb == "":
                continue
            if verb == "__sleep__":
                time.sleep(float(arg or "0"))
            elif verb == "file-open":
                opened = "100 60"
            elif verb in ("select-by-id", "select-all", "select-clear"):
                pass
            elif verb in (
                "path-union",
                "path-difference",
                "path-simplify",
                "path-combine",
                "path-break-apart",
                "path-intersection",
                "object-stroke-to-path",
            ):
                pass
            elif verb == "query-x":
                echoed.append("10")
            elif verb == "query-width":
                echoed.append("30")
            elif verb == "query-all":
                echoed.append("svg1,10,10,70,20")
                echoed.append("r1,10,10,30,20")
            elif verb == "export-type":
                export_type = arg or export_type
            elif verb == "export-filename":
                export_filename = arg
            elif verb == "export-width":
                export_width = int(arg or "0")
            elif verb in (
                "export-area-page",
                "export-plain-svg",
                "export-area-drawing",
                "export-id",
                "export-id-only",
            ):
                pass
            elif verb == "export-do":
                if export_filename:
                    out = Path(export_filename)
                    if export_type == "svg":
                        out.write_text(_SVG, encoding="utf-8")
                    else:
                        w = export_width or 100
                        h = round(w * 60 / 100) if export_width else 60
                        _write_png(out, w, h)
            else:
                # Unknown action: mirror the real stderr line; stdout still gets echo + prompt.
                sys.stderr.write(
                    f"InkscapeApplication::parse_actions: could not find action for: {verb}\n"
                )
                sys.stderr.flush()
        _ = opened
        sys.stdout.write(cmd + "\n")
        for line in echoed:
            sys.stdout.write(line + "\n")
        sys.stdout.write(_PROMPT)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
