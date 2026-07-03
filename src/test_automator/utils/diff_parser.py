"""Unified diff parser for extracting changed line numbers."""

from __future__ import annotations

import re


def parse_changed_lines(patch: str) -> set[int]:
    """Return new-file line numbers added or modified by the patch."""
    changed_lines: set[int] = set()
    current_line = 0

    for line in patch.splitlines():
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            if match:
                current_line = int(match.group(1)) - 1
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+"):
            current_line += 1
            changed_lines.add(current_line)
        elif line.startswith("-"):
            pass
        else:
            current_line += 1

    return changed_lines


def extract_diff_hunk_for_range(
    patch: str, line_start: int, line_end: int, context_lines: int = 2
) -> str:
    """Return ONLY the diff lines within ``line_start..line_end`` (in
    the new-file numbering).

    The result is a compact view of what changed inside a specific
    function. For a 60-line function where 2 lines were added at line
    142, this returns roughly:

        @@ around line 142
           Line 140: .setLocationDisplayCountyCovered(...)
           Line 141: .setCriminalRollUpSettings(...)
        +  Line 142: .setNotes(getSetting<String?>(Settings.Notes))
           Line 143: .build()

    This is what we feed to the LLM as the "what changed" portion of
    the prompt, separately from the full function body. The bot uses
    it to tell Claude to focus tests on the changed code.

    ``context_lines`` is how many unchanged lines to include before and
    after each changed chunk for readability.

    Returns an empty string if no changes fall in the given range
    (defensive — shouldn't happen if the caller passes the right
    range, but harmless if it does).
    """
    if not patch:
        return ""

    # Walk the patch tracking the new-file line number. Collect tuples of
    # (line_no, marker, text) where marker is ' ' (context), '+', or '-'.
    # For removed lines we keep the marker but record the line number of
    # the NEW file's neighboring line (since '-' lines have no new-file
    # line number).
    entries: list[tuple[int, str, str]] = []
    new_line = 0

    for line in patch.splitlines():
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            if match:
                new_line = int(match.group(1)) - 1
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+"):
            new_line += 1
            entries.append((new_line, "+", line[1:]))
        elif line.startswith("-"):
            # A removed line — record at "current" new_line position
            # so it appears in context with surrounding new-file lines
            entries.append((new_line, "-", line[1:]))
        else:
            new_line += 1
            entries.append((new_line, " ", line[1:] if line.startswith(" ") else line))

    # Find changed entries that fall inside [line_start, line_end]
    changed_in_range = [
        (n, m, t) for (n, m, t) in entries
        if m in ("+", "-") and line_start <= n <= line_end
    ]
    if not changed_in_range:
        return ""

    # Expand each change with context_lines of surrounding context.
    # We want unchanged lines immediately before/after change clusters.
    keep_lines: set[int] = set()
    for (n, _, _) in changed_in_range:
        keep_lines.add(n)
        for offset in range(1, context_lines + 1):
            keep_lines.add(n - offset)
            keep_lines.add(n + offset)

    # Build the output: include any entry whose line number is in
    # keep_lines OR which is a change inside the range
    output_lines: list[str] = []
    prev_line_no: int | None = None
    for n, marker, text in entries:
        in_range = line_start <= n <= line_end
        is_change_in_range = marker in ("+", "-") and in_range
        is_context_kept = marker == " " and n in keep_lines and in_range

        if is_change_in_range or is_context_kept:
            # Insert a separator if there's a gap in line numbers
            if prev_line_no is not None and n > prev_line_no + 1:
                output_lines.append("...")
            output_lines.append(f"{marker} {text}")
            prev_line_no = n

    return "\n".join(output_lines)


def extract_code_block(text: str) -> str:
    """Extract the first Python code block from markdown text."""
    pattern = re.compile(r"```(?:python)?\n(.*?)```", re.DOTALL)
    match = pattern.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()
