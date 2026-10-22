"""Compact, single-line rendering of pydantic validation errors.

Both the op parser (`edit.ops.parse_ops`) and the argument-validation middleware
(`tools._middleware`) use it, so every invalid-input message has the same shape:
``<where>: <message>; <where>: <message>; <where>: <message> (+N more)``. No pydantic URLs, no
echoed input values (sec.12), at most three problems.
"""

from __future__ import annotations

from typing import Any

MAX_SHOWN = 3


def loc_path(loc: tuple[int | str, ...], root: str | None = None) -> str:
    """`(3, "fill")` with root `ops` -> `ops[3].fill`; `("targets", 0)` -> `targets[0]`."""
    out = root or ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or root or "arguments"


def format_validation_errors(errors: list[Any], *, root: str | None = None) -> str:
    """The first three distinct `loc: msg` pairs of a pydantic `errors()` list, plus a `+N more`."""
    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[str, str]] = []
    for err in errors:
        pair = (loc_path(tuple(err["loc"]), root), str(err["msg"]))
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)
    shown = "; ".join(f"{where}: {msg}" for where, msg in pairs[:MAX_SHOWN])
    more = f" (+{len(pairs) - MAX_SHOWN} more)" if len(pairs) > MAX_SHOWN else ""
    return f"{shown}{more}"
