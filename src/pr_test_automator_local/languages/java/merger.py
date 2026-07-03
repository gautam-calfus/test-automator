"""Java test-file parsing and merge utilities.

For Acme specifically, this code is **rarely exercised** — the project
has no existing tests, so the fresh-generation path covers everything.
This module exists for the case where a test file is created and then
the bot is run again later, at which point incremental merge takes over.

Three responsibilities:
1. ``parse_existing_test_functions`` — find every ``@Test``-annotated
   method in a Java test file
2. ``merge_new_tests`` — splice new ``@Test`` methods into an existing
   file just before the class's closing brace
3. ``remove_tests`` — strip specific tests by name (used for stale-test
   removal)

Mirrors the structure of ``languages.kotlin.merger`` but adapted for
Java syntax (no backticked names, ``@Test void`` instead of ``@Test fun``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pr_test_automator_local.languages.java.analyzer import _get_parser


@dataclass
class JavaTestFunction:
    """A single ``@Test``-annotated method in a Java test file."""

    name: str
    line_start: int
    line_end: int
    annotations: list[str] = field(default_factory=list)


def parse_existing_test_functions(content: str) -> list[JavaTestFunction]:
    """Return all ``@Test``-annotated methods in a Java test file.

    Uses tree-sitter to walk the AST. For each ``method_declaration``
    that has ``@Test`` in its modifiers, records its name and line range.
    """
    if not content.strip():
        return []

    try:
        parser = _get_parser()
        source_bytes = content.encode("utf-8")
        tree = parser.parse(source_bytes)
    except Exception:
        return []

    results: list[JavaTestFunction] = []
    _walk_for_tests(tree.root_node, source_bytes, results)
    return results


def _walk_for_tests(node, source_bytes: bytes, results: list[JavaTestFunction]):
    """Recursively walk looking for @Test-annotated methods."""
    if node.type == "method_declaration":
        annotations = _method_annotations(node, source_bytes)
        if "Test" in annotations:
            name = _method_name(node, source_bytes)
            if name:
                results.append(
                    JavaTestFunction(
                        name=name,
                        line_start=node.start_point[0] + 1,
                        line_end=node.end_point[0] + 1,
                        annotations=annotations,
                    )
                )
        return  # don't recurse into method bodies
    for child in node.children:
        _walk_for_tests(child, source_bytes, results)


def _method_annotations(node, source_bytes: bytes) -> list[str]:
    """Return annotation names attached to a method declaration.

    For ``@Test``, returns ``["Test"]``.
    For ``@Test @DisplayName("x")``, returns ``["Test", "DisplayName"]``.
    """
    names: list[str] = []
    for child in node.children:
        if child.type == "modifiers":
            for mod in child.children:
                if mod.type in ("marker_annotation", "annotation"):
                    # The annotation name is the identifier after `@`
                    for inner in mod.children:
                        if inner.type == "identifier":
                            names.append(
                                source_bytes[inner.start_byte : inner.end_byte]
                                .decode("utf-8", errors="replace")
                            )
                            break
            break
    return names


def _method_name(node, source_bytes: bytes) -> str | None:
    """Find the method name identifier (after modifiers + return type)."""
    for child in node.children:
        if child.type in (
            "modifiers", "void_type", "integral_type",
            "floating_point_type", "boolean_type", "type_identifier",
            "generic_type", "array_type", "scoped_type_identifier",
            "type_parameters",
        ):
            continue
        if child.type == "identifier":
            return source_bytes[
                child.start_byte : child.end_byte
            ].decode("utf-8", errors="replace")
    return None


def extract_test_source(content: str, tests: list[JavaTestFunction]) -> str:
    """Return the verbatim source of the given tests in file order.

    Used by TestGenerator to show Claude what was removed (so it has a
    hint at the prior style for the same source functions).
    """
    if not tests or not content:
        return ""

    lines = content.splitlines(keepends=True)
    in_order = sorted(tests, key=lambda t: t.line_start)
    chunks: list[str] = []
    for t in in_order:
        # tree-sitter lines are 1-indexed; line_end is inclusive
        chunk = "".join(lines[t.line_start - 1 : t.line_end])
        chunks.append(chunk)
    return "\n".join(c.rstrip() for c in chunks)


def remove_tests(content: str, to_remove: list[JavaTestFunction]) -> str:
    """Return content with the specified tests removed.

    Operates line-by-line: deletes lines from each test's line_start
    through line_end. Preserves all other content (imports, fields,
    @BeforeEach, other tests).
    """
    if not to_remove:
        return content

    lines = content.splitlines(keepends=True)
    # Build a set of line numbers (1-indexed) to drop
    drop = set()
    for t in to_remove:
        for ln in range(t.line_start, t.line_end + 1):
            drop.add(ln)

    kept = [
        line for idx, line in enumerate(lines, start=1) if idx not in drop
    ]
    return "".join(kept)


def merge_new_tests(existing: str, new_tests: str) -> str:
    """Splice new ``@Test`` methods into the existing file just before
    the class's closing brace.

    ``new_tests`` should contain JUST the new method declarations (no
    package/imports/class wrapper) — see ``SYSTEM_PROMPT_INCREMENTAL``.
    The merger:
    1. Finds the class declaration's closing brace
    2. Inserts ``new_tests`` immediately before that brace
    3. Normalizes blank lines so we don't end up with 5 blank lines in a row

    Returns the merged content. If no class declaration can be found,
    returns ``existing + "\\n" + new_tests`` as a defensive fallback.
    """
    new_tests = new_tests.strip()
    if not new_tests:
        return existing

    # Find the top-level class's closing brace by brace-counting from
    # the first ``class`` or ``interface`` keyword.
    class_start = _find_class_body_start(existing)
    if class_start == -1:
        # No class declaration found — fallback append
        return existing.rstrip() + "\n\n" + new_tests + "\n"

    close_idx = _find_matching_close(existing, class_start)
    if close_idx == -1:
        return existing.rstrip() + "\n\n" + new_tests + "\n"

    before = existing[:close_idx].rstrip()
    after = existing[close_idx:]

    return f"{before}\n\n    {_indent_block(new_tests, 4)}\n{after}"


def _find_class_body_start(text: str) -> int:
    """Find the position of the ``{`` opening the top-level class body.

    Returns -1 if not found.
    """
    # Match: class | interface | enum | record  Name [...] {
    pattern = re.compile(
        r"\b(?:class|interface|enum|record)\s+\w+[^{]*\{", re.DOTALL
    )
    m = pattern.search(text)
    if m is None:
        return -1
    # Return position of the `{`
    return m.end() - 1


def _find_matching_close(text: str, open_brace_idx: int) -> int:
    """Brace-counting walker to find the closing ``}`` matching the
    opening ``{`` at ``open_brace_idx``. String/comment aware.

    Returns -1 if not found.
    """
    if open_brace_idx >= len(text) or text[open_brace_idx] != "{":
        return -1

    depth = 1
    i = open_brace_idx + 1
    n = len(text)

    while i < n:
        c = text[i]

        # Line comment
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        # Block comment
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
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1

    return -1


def _indent_block(text: str, spaces: int) -> str:
    """Indent each line of ``text`` (except the first) by ``spaces``
    spaces. The first line is left at its current position because
    ``merge_new_tests`` already places it at the correct indentation.
    """
    indent = " " * spaces
    lines = text.split("\n")
    if not lines:
        return ""
    return ("\n" + indent).join(lines)
