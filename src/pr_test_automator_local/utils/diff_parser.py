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


def extract_code_block(text: str) -> str:
    """Extract the first Python code block from markdown text."""
    pattern = re.compile(r"```(?:python)?\n(.*?)```", re.DOTALL)
    match = pattern.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()
