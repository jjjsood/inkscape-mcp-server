"""Font glyph-coverage diagnosis.

Reusable, read-only engine that answers "can this font family actually render these
characters?" from the FONT'S OWN cmap — never from fontconfig auto-substitution. Consumed by the
validation engine (`validate_document` glyph-coverage finding) and the `set_font` tool
(`coverage_ok` / `uncovered_chars` at apply time).
"""
