"""Python AST analysis. Moved from steps/code_analyzer.py during the v0.2.0
plugin refactor. Behaviour is identical to the pre-refactor implementation.
"""

from __future__ import annotations

import ast

from test_automator.models import AffectedFunction
from test_automator.utils.exceptions import CodeAnalyzerError


def extract_affected(
    source_code: str,
    file_path: str,
    changed_lines: set[int],
) -> list[AffectedFunction]:
    """Walk the AST and pick out functions/classes overlapping changed_lines.

    Returns the FULL function body (not just changed lines) for each match,
    so Claude has enough context to write meaningful tests.
    """
    try:
        tree = ast.parse(source_code)
    except SyntaxError as exc:
        raise CodeAnalyzerError(
            f"Syntax error in {file_path}: {exc}"
        ) from exc

    lines = source_code.splitlines()
    results: list[AffectedFunction] = []

    for node in ast.walk(tree):
        if not isinstance(
            node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ):
            continue

        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        if not any(node.lineno <= ln <= end for ln in changed_lines):
            continue

        kind = _node_kind(node)
        qualified = _qualified_name(node, tree)
        snippet = "\n".join(lines[node.lineno - 1 : end])

        results.append(
            AffectedFunction(
                file_path=file_path,
                name=node.name,
                qualified_name=qualified,
                kind=kind,
                source_code=snippet,
                line_start=node.lineno,
                line_end=end,
            )
        )

    return results


def _node_kind(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> str:
    if isinstance(node, ast.ClassDef):
        return "class"
    prefix = "async_" if isinstance(node, ast.AsyncFunctionDef) else ""
    return f"{prefix}function"


def _qualified_name(
    target: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    tree: ast.Module,
) -> str:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in ast.walk(node):
            if item is target and item is not node:
                return f"{node.name}.{target.name}"
    return target.name
