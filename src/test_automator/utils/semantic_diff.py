"""Detect whether two versions of a function differ SEMANTICALLY or
only by formatting.

Motivation: a formatter (Prettier, Black, ktlint, google-java-format)
rewriting a block marks every line as changed in git, so the analyzer
flags every function in that block as "changed" and we waste LLM calls
generating tests for functions whose behavior is identical. Comparing
normalized bodies lets us drop those.

Safety rule: the normalization must never make two SEMANTICALLY
different snippets look equal (that would drop a real change we should
have tested). It only erases formatting the language ignores:
- whitespace next to punctuation/operators (``(x)=>{`` vs ``(x) => {``)
  — but whitespace BETWEEN two word characters is kept, so ``return x``
  never collapses to ``returnx``;
- quote style (``"x"`` vs ``'x'``);
- statement terminators ``;`` and trailing commas before a closing
  bracket — both non-semantic in the supported languages;
- line breaks, for brace languages where they carry no meaning (so a
  one-line body and its multi-line reformat compare equal).

For indentation-significant languages (Python) line breaks and leading
indentation ARE preserved, so a re-indent that changes control flow is
NOT treated as formatting-only.
"""

from __future__ import annotations

import re

_TRAILING_COMMA_RE = re.compile(r",(?=\s*[)\]}])")


def _collapse_ws_and_quotes(s: str) -> str:
    """Unify quotes and drop whitespace that sits next to punctuation,
    keeping a single space only between two word characters."""
    s = s.replace('"', "'")
    out: list[str] = []
    for i, ch in enumerate(s):
        if ch.isspace():
            prev = out[-1] if out else ""
            nxt = s[i + 1] if i + 1 < len(s) else ""
            prev_word = prev.isalnum() or prev == "_"
            next_word = nxt.isalnum() or nxt == "_"
            if prev_word and next_word and prev != " ":
                out.append(" ")
            # else: whitespace adjacent to punctuation → insignificant
        else:
            out.append(ch)
    return "".join(out).strip()


def normalize(code: str, indent_significant: bool = False) -> str:
    """Normalize a snippet for formatting-insensitive comparison.

    ``indent_significant`` (Python) keeps line structure and each
    line's leading-indent width, so semantic re-indentation still
    registers as a change. For brace languages, line breaks are
    flattened away.
    """
    units: list[str] = []
    for raw in code.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue  # blank lines are never semantic
        body = _collapse_ws_and_quotes(stripped)
        if not body:
            continue
        if indent_significant:
            indent = len(raw) - len(raw.lstrip())
            units.append(f"{indent}:{body}")
        else:
            units.append(body)

    joined = "\n".join(units) if indent_significant else "".join(units)
    # Statement terminators and trailing commas are non-semantic.
    joined = joined.replace(";", "")
    joined = _TRAILING_COMMA_RE.sub("", joined)
    return joined


def only_formatting_changed(
    base_code: str, current_code: str, indent_significant: bool = False
) -> bool:
    """True when ``base_code`` and ``current_code`` are identical once
    formatting is normalized away — i.e. behavior is unchanged."""
    if not base_code or not current_code:
        return False
    return normalize(base_code, indent_significant) == normalize(
        current_code, indent_significant
    )
