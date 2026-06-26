"""Font glyph-coverage engine (E16-04, read-only).

Answers, per text element, whether a DECLARED font family can actually render the characters it
is applied to — computed from the font file's OWN cmap, never from fontconfig auto-substitution.
This closes the run's only correctness gap: JP text in a Latin-only family passed
``validate_document`` (``ok, 0 findings``) and ``inspect.fonts.available=true`` only because the
RENDER substituted a covering font at draw time, while the SAVED SVG still named the
non-covering family (tofu on a stricter renderer).

How substitution is avoided (the whole point):

- ``fc-match`` is used ONLY to LOCATE the file backing a NAMED family. ``fc-match`` substitutes a
  different family when the requested one is absent (e.g. ``NonexistentFont`` -> ``Noto Sans``);
  so we VERIFY the matched family equals the requested family (case-insensitive) and otherwise
  return "coverage unknown" — that case is owned by the ``missing_font`` check, and we refuse to
  let a substituted font's cmap masquerade as the requested family's.
- Coverage is then read from the VERIFIED-PRESENT family's own cmap via
  ``fc-list :family=<fam> charset`` (fontconfig reporting that family's file charset — NOT picking
  a different font; a ``.ttc`` collection yields one charset line per face, which we union). The
  charset is a list of hex codepoint ranges; a character is covered iff its codepoint falls in one.
- A covering-family SUGGESTION uses ``fc-list :charset=<cp> <cp> ...`` which returns only families
  whose ACTUAL cmap covers EVERY listed codepoint, so the suggestion is cmap-verified by
  construction (again: fontconfig as a cmap reader, not a substitution engine).

SAFETY (sec.12): every ``fc-*`` call is an arg list, never a shell string; the only client-derived
value that reaches argv is the font-family name and the codepoints of the document text, and the
family is charset-restricted by the caller (``normalize_font_family``) before it gets here. No host
path is ever returned in a finding — only family names and the uncovered characters themselves.
Degrades gracefully: if fontconfig is unavailable, or a family cannot be located, coverage is
reported as "unknown" (``None``) and the caller emits nothing rather than a false positive.
"""

from __future__ import annotations

import shutil
import unicodedata

from inkscape_mcp.document.inspect import GENERIC_FONT_KEYWORDS
from inkscape_mcp.logging_setup import get_logger
from inkscape_mcp.workspace.subprocess_exec import ProcessError, ProcessResult, run_process

_logger = get_logger("fonts.coverage")

#: Codepoints that are part of normal text but are never "glyphs a font must draw" — flagging them
#: as uncovered would be a false positive. ASCII space and the common Unicode whitespace; control
#: characters are excluded separately via the unicodedata category check.
_WHITESPACE = frozenset(
    {
        0x20,  # space
        0x09,  # tab
        0x0A,  # newline
        0x0D,  # carriage return
        0xA0,  # no-break space
        0x2028,  # line separator
        0x2029,  # paragraph separator
        0x200B,  # zero-width space
        0xFEFF,  # zero-width no-break space / BOM
    }
)


def _significant_codepoints(text: str) -> list[int]:
    """Codepoints from ``text`` that a font is actually expected to render, in first-seen order.

    Drops whitespace (``_WHITESPACE``) and Unicode control / format / separator characters
    (categories ``Cc``/``Cf``/``Cs``/``Zl``/``Zp``/``Zs``): these are layout, not glyphs, so a font
    lacking a visible glyph for them is not "missing coverage". Everything else — letters, CJK,
    punctuation, symbols — is significant. De-duplicated so a repeated character is checked once.
    """
    seen: dict[int, None] = {}
    for ch in text:
        cp = ord(ch)
        if cp in _WHITESPACE:
            continue
        category = unicodedata.category(ch)
        if category in ("Cc", "Cf", "Cs", "Zl", "Zp", "Zs"):
            continue
        seen.setdefault(cp, None)
    return list(seen)


def _parse_charset(charset: str) -> list[tuple[int, int]]:
    """Parse an ``fc-query %{charset}`` value into inclusive ``(lo, hi)`` codepoint ranges.

    The charset is space-separated hex tokens, each either a single codepoint (``3042``) or a range
    (``3041-3096``). Malformed tokens are skipped rather than failing the whole parse.
    """
    ranges: list[tuple[int, int]] = []
    for token in charset.split():
        token = token.strip()
        if not token:
            continue
        try:
            if "-" in token:
                lo_s, hi_s = token.split("-", 1)
                lo, hi = int(lo_s, 16), int(hi_s, 16)
            else:
                lo = hi = int(token, 16)
        except ValueError:
            continue
        if lo > hi:
            lo, hi = hi, lo
        ranges.append((lo, hi))
    return ranges


def _covers(ranges: list[tuple[int, int]], codepoint: int) -> bool:
    """True iff ``codepoint`` falls within any inclusive range in ``ranges``."""
    return any(lo <= codepoint <= hi for lo, hi in ranges)


def _fc(binary_name: str, args: list[str]) -> ProcessResult | None:
    """Run an ``fc-*`` helper as an arg list (sec.12), or ``None`` if it is unavailable / failed.

    Resolves the absolute binary via ``shutil.which`` so argv[0] is never a bare client string, and
    swallows every failure mode (binary absent, launch error, timeout, non-zero exit) into ``None``
    so a caller degrades to "coverage unknown" instead of crashing or reporting a false result.
    """
    binary = shutil.which(binary_name)
    if binary is None:
        return None
    try:
        result = run_process([binary, *args])
    except ProcessError as exc:
        _logger.info("%s launch failed", binary_name, extra={"detail": str(exc)})
        return None
    if result.timed_out or result.returncode != 0:
        return None
    return result


def family_charset(family: str) -> list[tuple[int, int]] | None:
    """Codepoint ranges the NAMED family's own font file actually covers, or ``None`` if unknown.

    Locates the file backing ``family`` with ``fc-match`` and VERIFIES the matched family equals the
    request (case-insensitive) — if fontconfig substituted a different family (the requested one is
    not installed), we return ``None`` so a substituted font's cmap is never mistaken for the
    requested family's coverage (that case is the ``missing_font`` check's job). The verified
    family's own charset is then read via ``fc-list :family=<fam> charset`` (the family's actual
    cmap, unioned across faces for a ``.ttc``). Returns ``None`` on any fontconfig fault or when the
    family is a generic CSS keyword (those always resolve).
    """
    fam = family.strip()
    if not fam or fam.lower() in GENERIC_FONT_KEYWORDS:
        return None

    matched = _fc("fc-match", ["-f", "%{family}|%{file}", fam])
    if matched is None:
        return None
    out = matched.stdout.strip()
    if "|" not in out:
        return None
    matched_family, _sep, _file = out.partition("|")
    # fc-match's %{family} may be a comma list of aliases; the request must be one of them, else
    # fontconfig substituted a DIFFERENT family — refuse to read its cmap as if it were ours.
    matched_aliases = {a.strip().lower() for a in matched_family.split(",") if a.strip()}
    if fam.lower() not in matched_aliases:
        return None

    # Read the located family's OWN charset directly from fontconfig's view of the font file. We
    # query by FAMILY (the verified-present name), so fontconfig reports that family's cmap; a
    # `.ttc` collection (e.g. Noto CJK) yields one charset line per face, which we union.
    queried = _fc("fc-list", [f":family={fam}", "charset"])
    if queried is None:
        return None
    ranges: list[tuple[int, int]] = []
    for line in queried.stdout.splitlines():
        line = line.strip()
        if line.startswith(":charset="):
            line = line[len(":charset=") :]
        ranges.extend(_parse_charset(line))
    return ranges or None


def uncovered_chars(family: str, text: str) -> str | None:
    """The significant characters in ``text`` that ``family``'s own cmap cannot render.

    Returns a string of the uncovered characters (first-seen order, de-duplicated, whitespace /
    control excluded), an empty string when the family covers everything, or ``None`` when coverage
    could not be determined (fontconfig unavailable, family not installed / substituted, or a
    generic keyword). ``None`` means "unknown" — the caller must NOT treat it as a failure.
    """
    codepoints = _significant_codepoints(text)
    if not codepoints:
        return ""  # nothing to render -> trivially covered
    ranges = family_charset(family)
    if ranges is None:
        return None
    missing = [cp for cp in codepoints if not _covers(ranges, cp)]
    return "".join(chr(cp) for cp in missing)


def suggest_covering_family(chars: str) -> str | None:
    """A family whose ACTUAL cmap covers EVERY character in ``chars``, or ``None`` if none is found.

    Uses ``fc-list :charset=<cp> <cp> ...`` — fontconfig returns only families whose own cmap covers
    all listed codepoints, so the suggestion is cmap-verified by construction (no substitution). The
    first non-generic family name is returned; generic-looking fallbacks (e.g. a bare ``sans-serif``
    alias) are skipped in favour of a concrete family. Returns ``None`` when ``chars`` is empty,
    fontconfig is unavailable, or nothing covers the set.
    """
    significant = _significant_codepoints(chars)
    if not significant:
        return None
    charset_arg = ":charset=" + " ".join(f"{cp:04x}" for cp in significant)
    queried = _fc("fc-list", [charset_arg, "family"])
    if queried is None:
        return None
    for line in queried.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # `fc-list ... family` prints `Family Name,Alias` per matching file; take the first alias.
        family = line.split(",", 1)[0].strip()
        if family and family.lower() not in GENERIC_FONT_KEYWORDS:
            return family
    return None


__all__ = [
    "family_charset",
    "suggest_covering_family",
    "uncovered_chars",
]
