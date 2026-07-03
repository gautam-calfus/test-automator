"""Extract Java source code from Claude's responses.

Two modes:

- ``extract_java_file(text)``: full-file extraction for fresh-generation
  mode. Expects a complete .java file (with ``package``, ``import``s, and
  a top-level class). Strips markdown fences and any prose preamble.

- ``extract_java_tests_block(text)``: extracts just ``@Test`` methods for
  incremental-merge mode. The merger splices these into an existing
  test file.

Both modes are designed for Claude's actual response patterns:
- Sometimes Claude wraps the file in ```` ```java ```` fences
- Sometimes Claude includes prose explanation alongside the code
- Sometimes Claude returns multiple fences (e.g., quoted source snippet
  + the actual fix)

For multi-fence cases, the extractor prefers the fence containing a
``package`` declaration (most likely to be the real file).
"""

from __future__ import annotations

import re


class ExtractionError(ValueError):
    """Raised when the LLM response contains no recognizable Java code."""


# Markdown fence: ```java\n...\n``` or ```\n...\n```
_FENCE_RE = re.compile(
    r"```(?:java)?\s*\n(.*?)\n?```",
    re.DOTALL | re.IGNORECASE,
)

# Java package declaration
_PACKAGE_RE = re.compile(r"^\s*package\s+[\w.]+\s*;", re.MULTILINE)


def _strip_markdown_fences(text: str) -> str:
    """If the text is wrapped in (or contains) ```java``` fences,
    return the content of the BEST fence:

    1. If any fence contains a ``package`` declaration, return that one
       (it's most likely the full file).
    2. Otherwise, return the LARGEST fence.
    3. If no fences, return the text as-is.
    """
    matches = list(_FENCE_RE.finditer(text))
    if not matches:
        return text

    for match in matches:
        content = match.group(1)
        if _PACKAGE_RE.search(content):
            return content

    largest = max(matches, key=lambda m: len(m.group(1)))
    return largest.group(1)


def extract_java_file(text: str) -> str:
    """Extract a complete Java source file from the LLM response.

    Returns just the Java source (no markdown fences, no prose) starting
    with ``package`` and ending with the class's closing brace.

    Raises ExtractionError if:
    - The response contains no ``package`` declaration
    - No matching top-level closing brace can be found
    """
    if not text or not text.strip():
        raise ExtractionError("LLM response was empty")

    cleaned = _strip_markdown_fences(text)

    pkg_match = _PACKAGE_RE.search(cleaned)
    if pkg_match is None:
        raise ExtractionError(
            "LLM response contains no `package` declaration — looks like "
            "pure prose or a refusal rather than Java source. First 200 "
            f"chars: {text[:200]!r}"
        )

    body = cleaned[pkg_match.start() :]

    end_idx = _find_top_level_closing_brace(body)
    if end_idx == -1:
        raise ExtractionError(
            "LLM response has a `package` declaration but no matching "
            "class-level closing brace. The response may be truncated. "
            f"Last 200 chars: {body[-200:]!r}"
        )

    return body[: end_idx + 1].rstrip() + "\n"


def _find_top_level_closing_brace(text: str) -> int:
    """Find the closing ``}`` that ends the outermost class declaration.

    Walks the text counting brace depth, but is aware of:
    - String literals (including triple-quoted text blocks, Java 15+)
    - Character literals
    - Line comments (``//``)
    - Block comments (``/* ... */``)
    """
    depth = 0
    i = 0
    n = len(text)
    first_brace_seen = False

    while i < n:
        c = text[i]

        # Line comment: skip to end of line
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue

        # Block comment: skip to */
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i < n - 1 and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue

        # Text block (Java 15+): """ ... """
        if c == '"' and i + 2 < n and text[i + 1] == '"' and text[i + 2] == '"':
            i += 3
            while i < n - 2 and not (
                text[i] == '"' and text[i + 1] == '"' and text[i + 2] == '"'
            ):
                if text[i] == "\\":
                    i += 2
                    continue
                i += 1
            i += 3
            continue

        # String literal
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\":
                    i += 2
                    continue
                i += 1
            i += 1
            continue

        # Char literal
        if c == "'":
            i += 1
            while i < n and text[i] != "'":
                if text[i] == "\\":
                    i += 2
                    continue
                i += 1
            i += 1
            continue

        if c == "{":
            depth += 1
            first_brace_seen = True
        elif c == "}":
            depth -= 1
            if first_brace_seen and depth == 0:
                return i

        i += 1

    return -1


def extract_java_tests_block(text: str) -> str:
    """Extract just the ``@Test`` method declarations for incremental
    merging into an existing test file.

    Walks the text from the first ``@Test`` annotation, collecting each
    method declaration (brace-counted) until we run out of methods.
    Returns the methods concatenated with blank lines between them.

    Raises ExtractionError if no ``@Test`` annotations are found.
    """
    if not text or not text.strip():
        raise ExtractionError("LLM response was empty")

    cleaned = _strip_markdown_fences(text)

    # Find the first @Test
    first_test = cleaned.find("@Test")
    if first_test == -1:
        raise ExtractionError(
            "LLM response contains no @Test annotation — expected a list "
            f"of test methods. First 200 chars: {text[:200]!r}"
        )

    # Start at the first @Test and walk forward, collecting @Test methods
    methods: list[str] = []
    i = first_test
    n = len(cleaned)

    while i < n:
        # Find next @Test (might be the same one we just consumed)
        test_idx = cleaned.find("@Test", i)
        if test_idx == -1:
            break

        # The annotation might have arguments: @Test, @Test(expected=...)
        # Skip any class-level annotations that aren't tests
        method_start = test_idx
        method_end = _find_method_end(cleaned, test_idx)
        if method_end == -1:
            # Couldn't find the method's closing brace; stop
            break

        methods.append(cleaned[method_start : method_end + 1])
        i = method_end + 1

    if not methods:
        raise ExtractionError(
            "Found @Test annotations but couldn't extract complete "
            "method bodies. The response may be malformed or truncated."
        )

    return "\n\n".join(m.strip() for m in methods)


def _find_method_end(text: str, start: int) -> int:
    """Starting from a ``@Test`` annotation, find the closing brace of
    the method that follows. Returns the index of the closing brace,
    or -1 if not found.

    Same string/comment awareness as ``_find_top_level_closing_brace``.
    """
    # First find the opening brace of the method body
    i = start
    n = len(text)
    while i < n and text[i] != "{":
        # Skip over annotation arguments to avoid confusing parens
        if text[i] == "(":
            depth = 1
            i += 1
            while i < n and depth > 0:
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                i += 1
            continue
        i += 1
    if i >= n:
        return -1

    # Now brace-count from this opening brace
    depth = 0
    while i < n:
        c = text[i]

        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i < n - 1 and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue

        # Text block
        if c == '"' and i + 2 < n and text[i + 1] == '"' and text[i + 2] == '"':
            i += 3
            while i < n - 2 and not (
                text[i] == '"' and text[i + 1] == '"' and text[i + 2] == '"'
            ):
                if text[i] == "\\":
                    i += 2
                    continue
                i += 1
            i += 3
            continue

        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\":
                    i += 2
                    continue
                i += 1
            i += 1
            continue
        if c == "'":
            i += 1
            while i < n and text[i] != "'":
                if text[i] == "\\":
                    i += 2
                    continue
                i += 1
            i += 1
            continue

        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i

        i += 1

    return -1
