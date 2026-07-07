"""JavaScript/TypeScript test-file parsing and merge utilities.

Parses an existing Jest/Vitest test file to find its ``test(...)`` /
``it(...)`` calls, removes a subset, and merges new tests in. Used by
the incremental flow when a source file already has a test file.

Unlike Kotlin (where tests are methods inside a class and merging means
inserting before the class's closing brace), Jest tests are plain call
expressions and are valid at the top level of the file — even when the
existing tests live inside ``describe`` blocks. So ``merge_new_tests``
simply appends the new blocks at the end of the file. That is the only
merge strategy that is correct for EVERY file shape (top-level tests,
single describe, nested describes) without a real JS parser-printer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from test_automator.languages.javascript.extractor import (
    find_matching_paren,
)

# A test call at the start of a line: test( / it( with optional
# modifiers. describe( is deliberately NOT matched here — a describe
# block is a grouping, not an individual test; removing one would
# remove unrelated tests inside it.
_TEST_DECL_RE = re.compile(
    r"^[ \t]*(?P<callee>test|it)"
    r"(?P<modifier>\.(?:each|skip|only|todo|concurrent|failing))?"
    r"\s*\(",
    re.MULTILINE,
)

# First string literal (the test title) — ' " or ` quoted.
_TITLE_RE = re.compile(r"""(['"`])((?:\\.|(?!\1).)*)\1""", re.DOTALL)


@dataclass
class JsTestFunction:
    """A single ``test(...)`` / ``it(...)`` call in an existing test file.

    Fields:
        name: the test title WITHOUT quotes (e.g. "camelCase converts
            snake_case keys"). For ``test.each`` the title is the
            template used per case (may contain ``%s`` / ``$var``).
        line_start: 1-indexed line of the test call
        line_end: 1-indexed line of the call's closing ``)`` (or ``;``)
        kind: "test" or "it"
    """

    name: str
    line_start: int
    line_end: int
    kind: str = "test"


def parse_existing_test_functions(content: str) -> list[JsTestFunction]:
    """Find every ``test(...)`` / ``it(...)`` call in a test file.

    Tests nested inside ``describe`` blocks are found too (the regex
    matches at any indentation). Returns tests sorted by position.
    Calls whose parens can't be matched (malformed file) are skipped
    rather than raising — parsing an existing file must never crash
    the pipeline.
    """
    results: list[JsTestFunction] = []

    for match in _TEST_DECL_RE.finditer(content):
        open_idx = content.index("(", match.start())
        close_idx = find_matching_paren(content, open_idx)
        if close_idx == -1:
            continue

        end = close_idx + 1
        # test.each([...])('title %s', fn) — absorb the second call.
        if match.group("modifier") == ".each":
            j = end
            while j < len(content) and content[j] in " \t":
                j += 1
            if j < len(content) and content[j] == "(":
                second_close = find_matching_paren(content, j)
                if second_close != -1:
                    end = second_close + 1
                    # For .each the title lives in the SECOND call.
                    open_idx = j
                    close_idx = second_close
        if end < len(content) and content[end] == ";":
            end += 1

        title_match = _TITLE_RE.search(content[open_idx : close_idx + 1])
        if title_match is None:
            continue

        line_start = content.count("\n", 0, match.start()) + 1
        line_end = content.count("\n", 0, end - 1) + 1
        results.append(
            JsTestFunction(
                name=title_match.group(2),
                line_start=line_start,
                line_end=line_end,
                kind=match.group("callee"),
            )
        )

    return sorted(results, key=lambda t: t.line_start)


def extract_test_source(content: str, tests: list[JsTestFunction]) -> str:
    """Return the verbatim source text for the given tests, separated by
    blank lines. Used in the incremental-merge prompt to show the model
    what was removed.
    """
    if not tests:
        return ""
    lines = content.splitlines(keepends=False)
    sections: list[str] = []
    for t in sorted(tests, key=lambda t: t.line_start):
        start_idx = max(t.line_start - 1, 0)
        end_idx = min(t.line_end, len(lines))
        sections.append("\n".join(lines[start_idx:end_idx]))
    return "\n\n".join(sections)


def remove_tests(content: str, tests_to_remove: list[JsTestFunction]) -> str:
    """Remove the given tests from the file content.

    Preserves imports, mocks, beforeEach/afterEach hooks, describe
    wrappers, and all tests not in the remove list. Consumes one blank
    separator line after each removed test to keep the file tidy.
    (Same line-range algorithm as the Kotlin merger.)
    """
    if not tests_to_remove:
        return content

    lines = content.splitlines(keepends=False)
    remove_ranges = sorted(
        ((t.line_start, t.line_end) for t in tests_to_remove),
        key=lambda r: r[0],
    )

    new_lines: list[str] = []
    line_idx = 0
    range_idx = 0

    while line_idx < len(lines):
        current = line_idx + 1
        if range_idx < len(remove_ranges):
            r_start, r_end = remove_ranges[range_idx]
            if current < r_start:
                new_lines.append(lines[line_idx])
                line_idx += 1
            elif r_start <= current <= r_end:
                line_idx += 1
                if current == r_end:
                    if line_idx < len(lines) and lines[line_idx].strip() == "":
                        line_idx += 1
                    range_idx += 1
            else:
                range_idx += 1
        else:
            new_lines.append(lines[line_idx])
            line_idx += 1

    result = "\n".join(new_lines)
    if content.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def merge_new_tests(existing: str, new_tests: str) -> str:
    """Append ``new_tests`` (one or more top-level test blocks) at the
    end of the existing file.

    Top-level ``test(...)`` calls are valid Jest/Vitest regardless of
    whether the existing tests are wrapped in ``describe`` blocks, so
    appending is always structurally correct. New imports are NOT
    handled here by design — the incremental prompt forbids them (the
    model must use the existing file's imports, or ``require`` inside
    the test body).
    """
    if not new_tests.strip():
        return existing
    return existing.rstrip() + "\n\n" + new_tests.strip() + "\n"
