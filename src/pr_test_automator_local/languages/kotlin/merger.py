"""Kotlin test-file parsing and merge utilities — Stage 4b.

Parses an existing ``XTests.kt`` to find its ``@Test`` functions,
removes a subset, and merges new tests in. Used by the incremental
merge flow when the bot sees a source file that already has a test
file.

The parser is tree-sitter-based (same grammar as the source analyzer)
but with different walking logic — tests are inside a class body and
have backticked names that look syntactically different from regular
identifiers.

Naming conventions assumed (matching Initech's ``UserServiceTests.kt``):
- Test methods are ``@Test``-annotated ``fun `backticked english`()``
- A test's backticked name starts with ``methodName()`` where
  ``methodName`` is the source function it covers (e.g.
  ```create() saves new user``` covers ``create``)
- The class has class-level mock declarations, slots, a ``@BeforeEach``
  setup, and the ``@Test`` methods — these must all be preserved when
  removing individual tests
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pr_test_automator_local.languages.kotlin.analyzer import _get_parser


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class KotlinTestFunction:
    """A single ``@Test fun`` declaration in an existing Kotlin test file.

    Fields:
        name: the backticked English name WITHOUT surrounding backticks
            (e.g. "create() saves new user", not "`create() saves new user`")
        line_start: 1-indexed start of the test (including ``@Test`` if
            present). Used for source-text extraction.
        line_end: 1-indexed end of the test's closing brace
        annotations: annotation names attached to the function (e.g.
            ["Test"]). Does not include parentheses or arguments.
    """

    name: str
    line_start: int
    line_end: int
    annotations: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def parse_existing_test_functions(content: str) -> list[KotlinTestFunction]:
    """Walk a Kotlin test file's AST and extract every ``@Test``-annotated
    function declaration.

    Returns tests sorted by their position in the file. Tests that lack
    an ``@Test`` annotation are skipped — those are helpers, not actual
    test methods.

    Backticked names have their backticks stripped: a function declared
    as ``fun `create() saves new user`()`` has name ``create() saves
    new user``. This makes downstream matching simpler.
    """
    parser = _get_parser()
    source_bytes = content.encode("utf-8")

    try:
        tree = parser.parse(source_bytes)
    except Exception:
        return []

    results: list[KotlinTestFunction] = []
    _walk_for_tests(tree.root_node, source_bytes, results)
    return sorted(results, key=lambda t: t.line_start)


def _walk_for_tests(
    node, source_bytes: bytes, results: list[KotlinTestFunction]
) -> None:
    """Walk to find ``function_declaration`` nodes inside class bodies,
    keep only those with ``@Test`` annotation.
    """
    if node.type == "function_declaration":
        annotations = _collect_function_annotations(node, source_bytes)
        if "Test" not in annotations:
            # Non-test function (e.g. helper, ``beforeEach``, etc.) — skip
            return
        name = _extract_function_name(node, source_bytes)
        if name is None:
            return
        # The function's line range. If the function has annotations
        # *inside* the modifiers child, those are already in the range.
        # Otherwise tree-sitter starts at ``fun`` and we'd lose ``@Test``
        # above. We use ``include_annotations_above`` to handle either.
        line_start, line_end = _function_line_range(
            node, source_bytes, annotations
        )
        results.append(
            KotlinTestFunction(
                name=name,
                line_start=line_start,
                line_end=line_end,
                annotations=annotations,
            )
        )
        return

    for child in node.children:
        _walk_for_tests(child, source_bytes, results)


def _collect_function_annotations(
    fn_node, source_bytes: bytes
) -> list[str]:
    """Return annotation names attached to a function declaration.

    Annotations live inside the function's ``modifiers`` child. Each
    ``annotation`` has text like ``@Test`` or ``@Suppress("foo")``. We
    strip the leading ``@`` and trailing argument list.
    """
    names: list[str] = []
    for child in fn_node.children:
        if child.type != "modifiers":
            continue
        for mod in child.children:
            if mod.type != "annotation":
                continue
            text = source_bytes[mod.start_byte : mod.end_byte].decode(
                "utf-8"
            )
            cleaned = text.lstrip("@").split("(")[0].strip()
            if cleaned:
                names.append(cleaned)
    return names


def _extract_function_name(fn_node, source_bytes: bytes) -> str | None:
    """Return the function name with any surrounding backticks stripped.

    Kotlin's grammar represents backticked identifiers as a single
    ``identifier`` node whose text includes the backticks. We strip them
    so downstream code matches names like ``create() saves new user``
    without worrying about the backtick wrapping.
    """
    for child in fn_node.children:
        if child.type == "modifiers":
            continue
        if child.type == "identifier":
            text = source_bytes[child.start_byte : child.end_byte].decode(
                "utf-8"
            )
            # Strip surrounding backticks if present
            if text.startswith("`") and text.endswith("`") and len(text) >= 2:
                return text[1:-1]
            return text
    return None


def _function_line_range(
    fn_node, source_bytes: bytes, annotations: list[str]
) -> tuple[int, int]:
    """Get the 1-indexed line range for a test function, including any
    annotation lines that appear directly above the function.

    The tree-sitter grammar usually nests annotations inside the
    function's ``modifiers`` node, so they're already included in the
    function's range. But annotation placement varies — some teams put
    ``@Test`` on a line by itself above the function. We need both
    cases handled.

    Strategy: start with tree-sitter's reported range. Then walk upward
    one line at a time, absorbing any line that's purely an annotation
    or blank space between annotations.
    """
    start = fn_node.start_point[0] + 1
    end = fn_node.end_point[0] + 1

    if not annotations:
        return start, end

    # If annotations are in the modifiers child, tree-sitter's start_point
    # already includes them. We don't need to expand. But check anyway —
    # if the line immediately above isn't already in our range AND looks
    # like an annotation, absorb it.
    source_lines = source_bytes.decode("utf-8").splitlines()

    line_idx = start - 2  # zero-indexed line ABOVE the start
    while line_idx >= 0:
        line = source_lines[line_idx].strip()
        if line.startswith("@") or line == "":
            line_idx -= 1
            continue
        break
    new_start = line_idx + 2  # convert back to 1-indexed, AFTER the non-annotation line

    return min(new_start, start), end


# ---------------------------------------------------------------------------
# Test source extraction
# ---------------------------------------------------------------------------


def extract_test_source(
    content: str, tests: list[KotlinTestFunction]
) -> str:
    """Return the verbatim source text for the given tests, separated by
    blank lines. Used in the incremental-merge prompt to show Claude
    what was removed.

    Tests are sorted by line number before extraction so the output
    matches their order in the original file.
    """
    if not tests:
        return ""
    lines = content.splitlines(keepends=False)
    sorted_tests = sorted(tests, key=lambda t: t.line_start)
    sections: list[str] = []
    for t in sorted_tests:
        # Convert 1-indexed inclusive to 0-indexed slice
        start_idx = max(t.line_start - 1, 0)
        end_idx = min(t.line_end, len(lines))
        section = "\n".join(lines[start_idx:end_idx])
        sections.append(section)
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Test removal (surgical edit of the existing file)
# ---------------------------------------------------------------------------


def remove_tests(
    content: str, tests_to_remove: list[KotlinTestFunction]
) -> str:
    """Remove the given tests from the file content.

    Preserves:
    - All imports, class-level properties, mock declarations, slots
    - ``@BeforeEach`` and other non-removed methods
    - All tests NOT in the remove list
    - The class's closing brace

    Removes:
    - Each test's annotation lines (``@Test``)
    - The function body
    - One blank line of separator AFTER each removed test (to keep the
      file tidy)
    """
    if not tests_to_remove:
        return content

    # Build a set of (start, end) line tuples to remove. Use 1-indexed
    # inclusive. We'll work in line-list form for surgical precision.
    lines = content.splitlines(keepends=False)
    remove_ranges = sorted(
        ((t.line_start, t.line_end) for t in tests_to_remove),
        key=lambda r: r[0],
    )

    # Build the new line list by walking the source. Skip lines that
    # fall inside any remove range.
    new_lines: list[str] = []
    line_idx = 0  # 0-indexed iterator over `lines`
    range_idx = 0

    while line_idx < len(lines):
        current_line_1based = line_idx + 1
        if range_idx < len(remove_ranges):
            r_start, r_end = remove_ranges[range_idx]
            if current_line_1based < r_start:
                new_lines.append(lines[line_idx])
                line_idx += 1
            elif r_start <= current_line_1based <= r_end:
                # Inside a removal range — skip
                line_idx += 1
                if current_line_1based == r_end:
                    # Just finished the range. Also consume one trailing
                    # blank line if there is one, to avoid double-blanks.
                    if line_idx < len(lines) and lines[line_idx].strip() == "":
                        line_idx += 1
                    range_idx += 1
            else:
                # current is past this range — advance the range iterator
                range_idx += 1
        else:
            new_lines.append(lines[line_idx])
            line_idx += 1

    # Preserve the trailing newline if the original had one
    result = "\n".join(new_lines)
    if content.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


# ---------------------------------------------------------------------------
# Merging new tests into the existing file
# ---------------------------------------------------------------------------


def merge_new_tests(existing: str, new_tests: str) -> str:
    """Insert ``new_tests`` (a string containing one or more
    ``@Test fun`` declarations) into the existing file's class body,
    right before the closing brace.

    ``new_tests`` is expected to be the inside-the-class text — just
    ``@Test`` annotations and function declarations, NOT a full Kotlin
    file with package/imports/class wrapper.

    Strategy:
    1. Find the LAST closing brace in the file (assumes it closes the
       test class, which is the standard structure)
    2. Insert new_tests before it with a blank line separator
    3. Indent new_tests to match the rest of the class body

    Edge cases:
    - If no closing brace is found (malformed file), append at the end
    - If new_tests is empty, return existing unchanged
    - Trailing whitespace is normalized
    """
    if not new_tests.strip():
        return existing

    # Detect class-body indentation by looking at any existing `@Test` or
    # `fun ` line. Initech uses 4-space indentation.
    indent = _detect_class_body_indent(existing)
    new_tests_indented = _reindent(new_tests.strip(), indent)

    # Find the position of the last `}` (the class's closing brace)
    last_brace_idx = existing.rfind("}")
    if last_brace_idx == -1:
        # Malformed — just append
        return existing.rstrip() + "\n\n" + new_tests_indented + "\n"

    before = existing[:last_brace_idx].rstrip()
    after = existing[last_brace_idx:]

    return (
        before
        + "\n\n"
        + new_tests_indented
        + "\n"
        + after
    )


def _detect_class_body_indent(content: str) -> str:
    """Return the indentation prefix used at class-body level (e.g. four
    spaces). Looks for any line that starts with ``@Test``, ``fun ``,
    ``private val``, or similar class-body markers.
    """
    for line in content.splitlines():
        # Match common class-body lines
        if re.match(r"^(\s+)(@Test|fun |private val |@BeforeEach)", line):
            return re.match(r"^(\s*)", line).group(1)
    # Default: four spaces
    return "    "


def _reindent(text: str, indent: str) -> str:
    """Re-indent a block of text so every line starts with ``indent``.

    The block's existing leading whitespace is normalized first by
    detecting the smallest non-empty leading whitespace, then replacing
    it with ``indent`` for every line.

    If lines don't share a common leading whitespace (e.g. Claude
    returned tests with no leading whitespace), all lines get ``indent``
    prepended.
    """
    lines = text.splitlines()
    if not lines:
        return text

    # Find the smallest non-empty leading whitespace across all non-blank
    # lines
    leading_ws_lengths = []
    for line in lines:
        if line.strip() == "":
            continue
        ws_len = len(line) - len(line.lstrip())
        leading_ws_lengths.append(ws_len)
    base_ws = min(leading_ws_lengths) if leading_ws_lengths else 0

    new_lines: list[str] = []
    for line in lines:
        if line.strip() == "":
            new_lines.append("")
            continue
        # Strip the base whitespace, then prepend the target indent
        stripped = line[base_ws:] if base_ws <= len(line) else line.lstrip()
        new_lines.append(indent + stripped)
    return "\n".join(new_lines)
