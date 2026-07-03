"""Parse a pytest test module to extract test function names and ranges."""

from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass
class TestFunction:
    name: str
    line_start: int
    line_end: int
    decorators: list[str]


def parse_test_functions(content: str) -> list[TestFunction]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []

    results: list[TestFunction] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue

        if node.decorator_list:
            start = min(d.lineno for d in node.decorator_list)
        else:
            start = node.lineno
        end = getattr(node, "end_lineno", node.lineno) or node.lineno

        decorator_names = [_decorator_name(d) for d in node.decorator_list]

        results.append(
            TestFunction(
                name=node.name,
                line_start=start,
                line_end=end,
                decorators=[d for d in decorator_names if d],
            )
        )

    return sorted(results, key=lambda t: t.line_start)


def covers(test_name: str, source_function_name: str) -> bool:
    if test_name == f"test_{source_function_name}":
        return True
    return test_name.startswith(f"test_{source_function_name}_")


def _decorator_name(decorator: ast.expr) -> str:
    if isinstance(decorator, ast.Name):
        return decorator.id
    if isinstance(decorator, ast.Attribute):
        parts: list[str] = []
        node: ast.expr = decorator
        while isinstance(node, ast.Attribute):
            parts.insert(0, node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.insert(0, node.id)
            return ".".join(parts)
    if isinstance(decorator, ast.Call):
        return _decorator_name(decorator.func)
    return ""
