"""Extract clean Kotlin source from LLM responses.

Claude Code's response to a "generate Kotlin tests" prompt can contain
any of these noise types alongside the actual code:

1. Markdown code fences: ```kotlin\n...\n```
2. Agent narration: "Now I have everything I need. Let me generate the tests."
3. Trailing commentary: "Notes on what each group covers: ..."
4. Section separators: ---
5. Tool-use narration: "The file write needs your approval..."

This module strips that noise and returns just the Kotlin code. Two
extractors:

- extract_kotlin_file for fresh-generation responses (full file:
  package + imports + class)
- extract_kotlin_tests_block for incremental responses (just
  @Test fun declarations, no class wrapper)

Both raise ExtractionError if they can't find Kotlin code in the
response — better to surface that loudly than to write prose to disk.
"""

from __future__ import annotations

import re


class ExtractionError(ValueError):
    """Raised when the LLM response contains no recognizable Kotlin code."""


# Markdown fence: ```kotlin\n...\n``` or ```\n...\n```
_FENCE_RE = re.compile(
    r"```(?:kotlin|kt|kts)?\s*\n(.*?)\n?```",
    re.DOTALL | re.IGNORECASE,
)


def _strip_markdown_fences(text: str) -> str:
    """If the text is wrapped in (or contains) ```kotlin``` fences,
    return the content of the BEST fence:

    1. If any fence contains a ``package`` declaration, return that one
       (it's most likely the full file the bot wants).
    2. Otherwise, return the LARGEST fence (most content).
    3. If no fences at all, return the text as-is.

    v0.2.0 fix: previously this returned the FIRST fence unconditionally.
    Claude sometimes prefixes its response with a small explanatory
    fence (e.g., quoting the source function it's discussing) BEFORE
    the actual full-file fence. The old logic picked the explanatory
    fence and failed because it had no ``package`` declaration. The
    new logic correctly picks the file-containing fence.
    """
    matches = list(_FENCE_RE.finditer(text))
    if not matches:
        return text

    # Prefer a fence that contains a `package` declaration — that's
    # almost certainly the actual Kotlin file (not a quoted snippet).
    package_decl = re.compile(r"^\s*package\s+[\w.]+", re.MULTILINE)
    for match in matches:
        content = match.group(1)
        if package_decl.search(content):
            return content

    # No fence has a `package` declaration. Fall back to the largest
    # fence — that's most likely the real content (snippets quoted
    # in passing are typically small).
    largest = max(matches, key=lambda m: len(m.group(1)))
    return largest.group(1)


# Match a Kotlin package declaration.
_PACKAGE_RE = re.compile(r"^\s*package\s+[\w.]+", re.MULTILINE)


def extract_kotlin_file(text: str) -> str:
    """Extract a complete Kotlin file (package + imports + class) from
    an LLM response.

    Strategy:
    1. Strip markdown fences if present
    2. Find the first `package` line — discard prose before it
    3. Find the matching class-level closing brace — discard prose after

    Raises ExtractionError if no `package` line is found OR if the
    class structure is malformed (no matching closing brace).
    """
    cleaned = _strip_markdown_fences(text)

    pkg_match = _PACKAGE_RE.search(cleaned)
    if pkg_match is None:
        raise ExtractionError(
            "LLM response contains no `package` declaration — looks like "
            "pure prose or a refusal rather than Kotlin source. First "
            f"200 chars: {text[:200]!r}"
        )

    body = cleaned[pkg_match.start() :]

    end_idx = _find_top_level_closing_brace(body)
    if end_idx == -1:
        raise ExtractionError(
            "LLM response has a `package` declaration but no matching "
            "class-level closing brace. The response may be truncated. "
            f"Last 200 chars: {body[-200:]!r}"
        )

    return body[: end_idx + 1].strip() + "\n"


def _find_top_level_closing_brace(text: str) -> int:
    """Return the index of the first `}` that closes the top-level
    block (class or object). Returns -1 if not found.

    We start at depth 0 and increment on `{`, decrement on `}`.
    The first time depth returns to 0 AFTER seeing at least one
    opening brace, we've found the close.

    Aware of string literals and comments so braces inside them don't
    count.
    """
    i = 0
    depth = 0
    n = len(text)
    saw_open = False

    while i < n:
        ch = text[i]

        # Line comment
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            i = nl + 1 if nl != -1 else n
            continue

        # Block comment
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            close = text.find("*/", i + 2)
            i = close + 2 if close != -1 else n
            continue

        # Triple-quoted string
        if text.startswith('"""', i):
            close = text.find('"""', i + 3)
            i = close + 3 if close != -1 else n
            continue

        # Single-line string (handles escapes)
        if ch == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                else:
                    i += 1
            i += 1
            continue

        # Character literal
        if ch == "'":
            i += 1
            while i < n and text[i] != "'":
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                else:
                    i += 1
            i += 1
            continue

        # Backtick-quoted identifier: Kotlin lets test names be escaped
        # identifiers like ``fun `saves {x} when empty`()`` — braces
        # inside them are part of the NAME, not real blocks, so skip to
        # the closing backtick. (Asurint's convention is backticked
        # English test names, so this is common here.)
        if ch == "`":
            close = text.find("`", i + 1)
            i = close + 1 if close != -1 else n
            continue

        # Real brace counting
        if ch == "{":
            depth += 1
            saw_open = True
        elif ch == "}":
            depth -= 1
            if depth == 0 and saw_open:
                return i

        i += 1

    return -1


# Match `@Test` annotation at the start of a (possibly indented) line.
_TEST_ANNOTATION_RE = re.compile(r"^\s*@Test\b", re.MULTILINE)


def extract_kotlin_tests_block(text: str) -> str:
    """Extract `@Test fun` declarations from an LLM response.

    Used for incremental merge — Claude was asked to return JUST the
    new tests, without a class wrapper. This extractor finds the actual
    `@Test` declarations and returns them concatenated.

    Strategy:
    1. Strip markdown fences if present
    2. Find the first `@Test` annotation
    3. From there, walk forward extracting complete `@Test fun ... {...}`
       blocks (tracking brace depth to find each function's end)
    4. Stop when we hit content that's clearly not Kotlin

    Raises ExtractionError if no `@Test` annotation is found.
    """
    cleaned = _strip_markdown_fences(text)

    first_test = _TEST_ANNOTATION_RE.search(cleaned)
    if first_test is None:
        raise ExtractionError(
            "LLM response contains no `@Test` annotation — incremental "
            "merge expected one or more @Test functions. First 200 "
            f"chars: {text[:200]!r}"
        )

    body = cleaned[first_test.start() :]
    blocks: list[str] = []

    i = 0
    while i < len(body):
        # Skip leading whitespace
        while i < len(body) and body[i] in " \t\n":
            i += 1
        if i >= len(body):
            break

        # Look for the next @Test annotation at this position
        remaining = body[i:]
        if not re.match(r"@Test\b", remaining):
            # Not at a @Test — we've hit prose. Stop.
            break

        # Find the next `{`
        open_brace_idx = body.find("{", i)
        if open_brace_idx == -1:
            break

        close_idx = _find_matching_brace(body, open_brace_idx)
        if close_idx == -1:
            break

        block = body[i : close_idx + 1].rstrip()
        blocks.append(block)
        i = close_idx + 1

    if not blocks:
        raise ExtractionError(
            "Found @Test annotation but couldn't extract any complete "
            "function blocks. The response may be malformed. First 200 "
            f"chars: {text[:200]!r}"
        )

    return "\n\n".join(blocks) + "\n"


def _find_matching_brace(text: str, open_idx: int) -> int:
    """Given the index of an opening `{`, return the matching `}` index,
    or -1 if not found. String/comment aware.
    """
    if open_idx >= len(text) or text[open_idx] != "{":
        return -1

    depth = 1
    i = open_idx + 1
    n = len(text)

    while i < n:
        ch = text[i]

        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            i = nl + 1 if nl != -1 else n
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            close = text.find("*/", i + 2)
            i = close + 2 if close != -1 else n
            continue

        if text.startswith('"""', i):
            close = text.find('"""', i + 3)
            i = close + 3 if close != -1 else n
            continue
        if ch == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                else:
                    i += 1
            i += 1
            continue
        if ch == "'":
            i += 1
            while i < n and text[i] != "'":
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                else:
                    i += 1
            i += 1
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i

        i += 1

    return -1
