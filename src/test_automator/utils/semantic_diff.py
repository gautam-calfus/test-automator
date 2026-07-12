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
- whitespace that sits next to punctuation/operators (Prettier's
  ``(x)=>{`` vs ``(x) => {``) — but whitespace BETWEEN two word
  characters is kept, so ``return x`` never collapses to ``returnx``;
- quote style (``"x"`` vs ``'x'``);
- trailing ``;`` / ``,`` (optional in JS objects/arrays, no-op in
  general).

For indentation-significant languages (Python) leading indentation is
preserved as part of each line, so a re-indent that changes control
flow is NOT treated as formatting-only.
"""

from __future__ import annotations


def _collapse_line(s: str) -> str:
    """Normalize a single already-stripped line."""
    s = s.replace('"', "'")  # unify quote style
    out: list[str] = []
    for i, ch in enumerate(s):
        if ch.isspace():
            prev = out[-1] if out else ""
            nxt = s[i + 1] if i + 1 < len(s) else ""
            prev_word = prev.isalnum() or prev == "_"
            next_word = nxt.isalnum() or nxt == "_"
            # Keep a single space ONLY between two word characters
            # (preserves ``return x``, ``else if``, Python keywords).
            if prev_word and next_word and prev != " ":
                out.append(" ")
            # otherwise the whitespace is insignificant → drop it
        else:
            out.append(ch)
    result = "".join(out).strip()
    # Trailing separators that formatters add/remove freely.
    while result and result[-1] in ";,":
        result = result[:-1]
    return result


def normalize(code: str, indent_significant: bool = False) -> str:
    """Normalize a snippet for formatting-insensitive comparison.

    ``indent_significant`` (Python) keeps each line's leading-indent
    width so a semantic re-indent still registers as a change.
    """
    lines_out: list[str] = []
    for raw in code.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue  # blank lines are never semantic
        body = _collapse_line(stripped)
        if not body:
            continue
        if indent_significant:
            indent = len(raw) - len(raw.lstrip())
            lines_out.append(f"{indent}:{body}")
        else:
            lines_out.append(body)
    return "\n".join(lines_out)


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
