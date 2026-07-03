"""Tree-sitter-based analyzer for Java source files.

Finds method declarations whose line ranges overlap the ``changed_lines``
set from the diff. Also extracts class signatures (declaration headers
without method bodies) for inclusion in LLM prompts — this prevents
Claude from hallucinating constructor parameters or method signatures.

Designed for Acme's Java code style:
- Spring ``@Service`` classes with constructor injection
- DAO aggregator pattern (e.g., ``daos.getQuestionDao()``)
- JUnit 5 + Mockito for tests

Tree-sitter node types we care about:
- ``method_declaration`` — regular methods
- ``constructor_declaration`` — constructors
- ``class_declaration`` — top-level classes
- ``interface_declaration`` — interfaces
- ``enum_declaration`` — enums
- ``record_declaration`` — records (Java 14+)
"""

from __future__ import annotations

import re
from functools import lru_cache

from test_automator.models import AffectedFunction


@lru_cache(maxsize=1)
def _get_parser():
    """Load and cache the tree-sitter Java parser.

    Lazy-loaded so users who don't touch Java files never pay the import
    cost. ``functools.lru_cache`` ensures we only build the parser once
    per process.
    """
    import tree_sitter_java
    from tree_sitter import Language, Parser

    lang = Language(tree_sitter_java.language())
    return Parser(lang)


def extract_affected(
    source_code: str,
    file_path: str,
    changed_lines: set[int],
) -> list[AffectedFunction]:
    """Return method/constructor declarations whose line range overlaps
    ``changed_lines``.

    Each AffectedFunction carries:
    - The method name (e.g., ``forceRouteToUsers``)
    - The fully-qualified name (``com.acme.service.CMService.forceRouteToUsers``)
    - ``kind``: "method" for methods inside a class, "constructor" for
      constructors, "function" for top-level (rare in Java; only seen in
      package-private utility files with static methods)
    - The full source code of the method body (used in fresh-mode prompts;
      diff-mode prompts may compact this further)
    - Line range (1-indexed)
    """
    if not source_code.strip() or not changed_lines:
        return []

    try:
        parser = _get_parser()
        source_bytes = source_code.encode("utf-8")
        tree = parser.parse(source_bytes)
    except Exception:
        return []

    package = _extract_package(tree.root_node, source_bytes)

    results: list[AffectedFunction] = []
    _walk(
        tree.root_node,
        source_bytes,
        changed_lines,
        package,
        class_stack=[],
        results=results,
        file_path=file_path,
    )
    return results


def _walk(
    node,
    source_bytes: bytes,
    changed_lines: set[int],
    package: str,
    class_stack: list[str],
    results: list[AffectedFunction],
    file_path: str,
) -> None:
    """Recursively walk the AST collecting methods/constructors whose
    line range overlaps changed_lines.

    Tracks the enclosing class names via ``class_stack`` so we can
    build fully-qualified names like
    ``com.acme.service.CMService.forceRouteToUsers``.
    """
    ntype = node.type

    # Track entry into a class/interface/enum/record so children's
    # qualified names include the enclosing class
    if ntype in (
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
    ):
        class_name = _first_identifier_text(node, source_bytes)
        new_stack = class_stack + [class_name] if class_name else class_stack
        for child in node.children:
            _walk(
                child, source_bytes, changed_lines, package,
                new_stack, results, file_path,
            )
        return

    if ntype == "method_declaration":
        _maybe_emit(
            node, source_bytes, changed_lines, package,
            class_stack, results, file_path, kind="method",
        )
        return

    if ntype == "constructor_declaration":
        _maybe_emit(
            node, source_bytes, changed_lines, package,
            class_stack, results, file_path, kind="constructor",
        )
        return

    # Otherwise just recurse
    for child in node.children:
        _walk(
            child, source_bytes, changed_lines, package,
            class_stack, results, file_path,
        )


def _maybe_emit(
    node,
    source_bytes: bytes,
    changed_lines: set[int],
    package: str,
    class_stack: list[str],
    results: list[AffectedFunction],
    file_path: str,
    kind: str,
) -> None:
    """If the node's line range overlaps changed_lines, emit an
    AffectedFunction.
    """
    line_start = node.start_point[0] + 1  # tree-sitter is 0-indexed
    line_end = node.end_point[0] + 1

    # Quick overlap check: does ANY changed line fall in [line_start, line_end]?
    if not any(line_start <= cl <= line_end for cl in changed_lines):
        return

    name = _first_identifier_text(node, source_bytes)
    if not name:
        return

    # For constructors, tree-sitter uses the class name as the constructor
    # name. That's fine; the qualified name will be ClassName.ClassName.
    qualified_parts = ([package] if package else []) + class_stack + [name]
    qualified_name = ".".join(qualified_parts)

    source = source_bytes[node.start_byte : node.end_byte].decode(
        "utf-8", errors="replace"
    )

    results.append(
        AffectedFunction(
            file_path=file_path,
            name=name,
            qualified_name=qualified_name,
            kind=kind,
            source_code=source,
            line_start=line_start,
            line_end=line_end,
        )
    )


def _first_identifier_text(node, source_bytes: bytes) -> str | None:
    """Return the text of the first ``identifier`` child of node.

    For ``method_declaration``, ``constructor_declaration``,
    ``class_declaration``, etc., the method/class name is a direct
    ``identifier`` child after any modifiers and return type.

    Skips identifiers inside ``modifiers`` (annotation names like ``@Override``)
    and inside any type nodes.
    """
    for child in node.children:
        if child.type == "modifiers":
            continue
        # Skip type-related nodes that come before the name in method
        # declarations: void/int/String/generic type/etc.
        if child.type in (
            "void_type",
            "integral_type",
            "floating_point_type",
            "boolean_type",
            "type_identifier",
            "generic_type",
            "array_type",
            "scoped_type_identifier",
            "type_parameters",
        ):
            continue
        if child.type == "identifier":
            return source_bytes[child.start_byte : child.end_byte].decode(
                "utf-8", errors="replace"
            )
    return None


_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)


def _extract_package(root, source_bytes: bytes) -> str:
    """Return the file's package name (e.g. ``com.acme.service``) or
    empty string if no package declaration is found.

    We walk the AST looking for ``package_declaration`` rather than
    using a regex on raw source — this is more robust against comments
    or strings that happen to contain "package ".
    """
    for child in root.children:
        if child.type == "package_declaration":
            # The package name is a scoped_identifier or identifier child
            for grand in child.children:
                if grand.type in ("scoped_identifier", "identifier"):
                    return source_bytes[
                        grand.start_byte : grand.end_byte
                    ].decode("utf-8", errors="replace")
    return ""


# ---------------------------------------------------------------------------
# Class signature extraction — same pattern as Kotlin's analyzer.
# Returns class declarations WITHOUT method bodies, for inclusion in the
# LLM prompt. This prevents Claude from hallucinating constructor params.
# ---------------------------------------------------------------------------


def extract_class_signatures(source_code: str, compact: bool = True) -> str:
    """Return a string with all class/interface/enum/record declarations
    in the file.

    With ``compact=True`` (the default), only emits:
    - Class-level annotations (@Service, @Component, etc.)
    - Visibility + class keyword + name + generics
    - Extends/implements clauses
    - Field declarations (the ``private final XxxDao xxxDao;`` style)
    - Constructor declarations (signatures + bodies — Claude often needs
      to see what gets assigned to fields)
    - Method signatures are OMITTED — Claude sees the method under test
      in ``functions_code``; including all other method signatures would
      blow the prompt token budget on a 5000-line file like CMService.

    With ``compact=False``, also emits method signatures with bodies
    replaced by ``{ ... }``. Useful for very small files where the full
    picture fits in the prompt.

    Returns empty string on parse failure or empty file.
    """
    if not source_code.strip():
        return ""

    try:
        parser = _get_parser()
        source_bytes = source_code.encode("utf-8")
        tree = parser.parse(source_bytes)
    except Exception:
        return ""

    signatures: list[str] = []
    for child in tree.root_node.children:
        if child.type in (
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "record_declaration",
        ):
            sig = _render_class_signature(child, source_bytes, compact=compact)
            if sig:
                signatures.append(sig)
    return "\n\n".join(signatures)


def _render_class_signature(node, source_bytes: bytes, compact: bool = True) -> str:
    """Render one class declaration. See ``extract_class_signatures`` for
    the ``compact`` semantics.
    """
    body = None
    for child in node.children:
        if child.type in ("class_body", "enum_body", "interface_body"):
            body = child
            break

    if body is None:
        return source_bytes[node.start_byte : node.end_byte].decode(
            "utf-8", errors="replace"
        ).strip()

    header = source_bytes[node.start_byte : body.start_byte + 1].decode(
        "utf-8", errors="replace"
    )

    member_lines: list[str] = []
    for member in body.children:
        if member.type == "{" or member.type == "}":
            continue
        # In compact mode, skip method declarations entirely. Keep fields,
        # constructors, nested classes, and static blocks.
        if compact and member.type == "method_declaration":
            continue
        member_text = _render_member(member, source_bytes, compact=compact)
        if member_text:
            member_lines.append(member_text)

    inner = "\n".join(f"    {line}" for line in "\n".join(member_lines).split("\n"))
    return f"{header}\n{inner}\n}}"


def _render_member(node, source_bytes: bytes, compact: bool = True) -> str:
    """Render a single class body member."""
    ntype = node.type
    full_text = source_bytes[node.start_byte : node.end_byte].decode(
        "utf-8", errors="replace"
    )

    if ntype in ("method_declaration", "constructor_declaration"):
        for child in node.children:
            if child.type == "block":
                head = source_bytes[
                    node.start_byte : child.start_byte
                ].decode("utf-8", errors="replace").rstrip()
                # For constructors, we usually want to see the body
                # (it shows what's assigned to fields). For methods,
                # replace body with `{ ... }`.
                if ntype == "constructor_declaration":
                    body_text = source_bytes[
                        child.start_byte : child.end_byte
                    ].decode("utf-8", errors="replace")
                    return f"{head} {body_text}"
                return f"{head} {{ ... }}"
        return full_text.strip()

    if ntype in (
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
    ):
        return _render_class_signature(node, source_bytes, compact=compact)

    return full_text.strip()
