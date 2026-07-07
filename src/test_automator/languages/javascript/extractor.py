"""Extract clean JavaScript/TypeScript source from LLM responses.

Same noise problem as the Kotlin extractor: responses may contain prose
preambles, trailing commentary, and markdown fences. Two extractors:

- ``extract_js_file`` for fresh-generation and fix responses (a
  complete test file: imports + tests)
- ``extract_js_tests_block`` for incremental responses (just
  ``test(...)`` / ``it(...)`` / ``describe(...)`` blocks, no imports)

Both raise ExtractionError if no usable code is found.

The block scanner is JS-aware: single/double-quoted strings, template
literals (including nested ``${...}`` expressions), and comments are
skipped when matching parens/braces. Regex literals are NOT modeled —
a ``/(/`` inside a regex could in principle confuse the scanner, but
regex literals are rare in test titles/bodies and the failure mode is
a loud ExtractionError, not silent corruption.
"""

from __future__ import annotations

import re


class ExtractionError(ValueError):
    """Raised when the LLM response contains no recognizable JS/TS code."""


# Markdown fence: ```js\n...\n``` etc.
_FENCE_RE = re.compile(
    r"```(?:javascript|typescript|jsx?|tsx?|mjs|cjs)?\s*\n(.*?)\n?```",
    re.DOTALL | re.IGNORECASE,
)

# A test call at the start of a (possibly indented) line:
# test( / it( / describe( with optional .each/.skip/... modifiers.
_TEST_CALL_RE = re.compile(
    r"^\s*(?:test|it|describe)"
    r"(?:\.(?:each|skip|only|todo|concurrent|failing))?\s*\(",
    re.MULTILINE,
)

# Lines that plausibly start a JS/TS file.
_CODE_ANCHOR_RE = re.compile(
    r"^(?:import\s|export\s|const\s|let\s|var\s|function\s|class\s|"
    r"async\s|type\s|interface\s|enum\s|"
    r"require\(|jest\.|vi\.|describe\(|test\(|it\(|"
    r"/\*|//|['\"]use strict['\"])",
    re.MULTILINE,
)


def _strip_markdown_fences(text: str) -> str:
    """Return the content of the BEST fence, or the text unchanged if
    there are no fences.

    Preference order (same rationale as the Kotlin extractor's
    package-declaration preference — pick the fence that is the actual
    deliverable, not a quoted snippet):
    1. A fence containing a test call (test(/it(/describe()
    2. Otherwise the largest fence
    """
    matches = list(_FENCE_RE.finditer(text))
    if not matches:
        return text

    for match in matches:
        if _TEST_CALL_RE.search(match.group(1)):
            return match.group(1)

    return max(matches, key=lambda m: len(m.group(1))).group(1)


def extract_js_file(text: str) -> str:
    """Extract a complete JS/TS test file from an LLM response.

    Strategy:
    1. Strip markdown fences if present
    2. Find the first code-looking line — discard prose before it
    3. Trim trailing prose lines (lines after the last code-ish line)

    Raises ExtractionError if no code anchor or no test call is found —
    a "test file" without a single test(/it(/describe( is prose or a
    refusal.
    """
    cleaned = _strip_markdown_fences(text)

    anchor = _CODE_ANCHOR_RE.search(cleaned)
    if anchor is None:
        raise ExtractionError(
            "LLM response contains no recognizable JavaScript/TypeScript "
            "code (no import/const/function/test line found). First 200 "
            f"chars: {text[:200]!r}"
        )

    body = cleaned[anchor.start() :]

    if not _TEST_CALL_RE.search(body):
        raise ExtractionError(
            "LLM response contains code-like lines but no test(/it(/"
            "describe( call — it doesn't look like a test file. First "
            f"200 chars: {text[:200]!r}"
        )

    body = _trim_trailing_prose(body)
    return body.strip() + "\n"


def _trim_trailing_prose(body: str) -> str:
    """Drop trailing lines that don't look like code.

    Walk backwards from the end; the first line that ends in a
    code-terminating character (``;``, ``}``, ``)``, `` ` ``) marks the
    real end of the file. Blank lines and comment lines above it are
    fine — we only trim BELOW the last code line.
    """
    lines = body.splitlines()
    last_code_idx = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.endswith((";", "}", ")", "`", "{", ",")):
            last_code_idx = i
    if last_code_idx == -1:
        return body
    return "\n".join(lines[: last_code_idx + 1])


def extract_js_tests_block(text: str) -> str:
    """Extract ``test(...)`` / ``it(...)`` / ``describe(...)`` blocks
    from an LLM response.

    Used for incremental merge — the model was asked to return JUST new
    test blocks, no imports and no file wrapper. Finds the first test
    call and walks forward extracting complete call statements (paren-
    matched, string/template/comment aware).

    Raises ExtractionError if no test call is found.
    """
    cleaned = _strip_markdown_fences(text)

    first = _TEST_CALL_RE.search(cleaned)
    if first is None:
        raise ExtractionError(
            "LLM response contains no test(/it(/describe( call — "
            "incremental merge expected one or more test blocks. First "
            f"200 chars: {text[:200]!r}"
        )

    body = cleaned[first.start() :]
    blocks: list[str] = []
    i = 0
    n = len(body)

    while i < n:
        while i < n and body[i] in " \t\n":
            i += 1
        if i >= n:
            break

        match = _TEST_CALL_RE.match(body[i:])
        if match is None:
            break  # hit prose — stop

        open_idx = body.find("(", i)
        if open_idx == -1:
            break
        close_idx = find_matching_paren(body, open_idx)
        if close_idx == -1:
            break

        end = close_idx + 1
        # test.each([...])(...) — the first call returns a function that
        # is immediately called with the title+body. Absorb the second
        # call too.
        j = end
        while j < n and body[j] in " \t":
            j += 1
        if j < n and body[j] == "(":
            second_close = find_matching_paren(body, j)
            if second_close != -1:
                end = second_close + 1

        if end < n and body[end] == ";":
            end += 1

        blocks.append(body[i:end].rstrip())
        i = end

    if not blocks:
        raise ExtractionError(
            "Found a test call but couldn't extract any complete blocks "
            "— the response may be truncated. First 200 chars: "
            f"{text[:200]!r}"
        )

    return "\n\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# JS-aware paren matching
# ---------------------------------------------------------------------------


def find_matching_paren(text: str, open_idx: int) -> int:
    """Given the index of an opening ``(``, return the index of its
    matching ``)``, or -1.

    Skips content inside single/double-quoted strings, template
    literals (tracking nested ``${...}`` expressions), and comments.
    """
    if open_idx >= len(text) or text[open_idx] != "(":
        return -1

    depth = 0
    # Context stack. Each entry is "code" or "template". Inside a
    # template, ``${`` pushes a new "code" context and its matching
    # ``}`` pops back to the template. Brace depth is tracked per code
    # context so we know WHICH ``}`` pops.
    contexts: list[dict] = [{"kind": "code", "brace_depth": 0}]
    i = open_idx
    n = len(text)

    while i < n:
        ctx = contexts[-1]
        ch = text[i]

        if ctx["kind"] == "template":
            if ch == "\\":
                i += 2
                continue
            if ch == "`":
                contexts.pop()
                i += 1
                continue
            if ch == "$" and i + 1 < n and text[i + 1] == "{":
                contexts.append({"kind": "code", "brace_depth": 0})
                i += 2
                continue
            i += 1
            continue

        # --- code context ---
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            i = nl + 1 if nl != -1 else n
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            close = text.find("*/", i + 2)
            i = close + 2 if close != -1 else n
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < n and text[i] != quote:
                if text[i] == "\\":
                    i += 2
                else:
                    i += 1
            i += 1
            continue
        if ch == "`":
            contexts.append({"kind": "template"})
            i += 1
            continue
        if ch == "{":
            ctx["brace_depth"] += 1
            i += 1
            continue
        if ch == "}":
            if ctx["brace_depth"] == 0 and len(contexts) > 1:
                # This closes a ${...} template expression.
                contexts.pop()
                i += 1
                continue
            ctx["brace_depth"] = max(0, ctx["brace_depth"] - 1)
            i += 1
            continue
        if ch == "(" and len(contexts) == 1:
            depth += 1
        elif ch == ")" and len(contexts) == 1:
            depth -= 1
            if depth == 0:
                return i
        i += 1

    return -1
