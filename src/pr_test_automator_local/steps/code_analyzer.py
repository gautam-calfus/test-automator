"""Step 2: Identify functions/classes affected by the diff using AST."""

from __future__ import annotations

import ast
import os

from pr_test_automator_local._logging import get_logger
from pr_test_automator_local.config import LocalTestConfig
from pr_test_automator_local.models import AffectedFunction, PRFile
from pr_test_automator_local.utils.diff_parser import parse_changed_lines
from pr_test_automator_local.utils.exceptions import CodeAnalyzerError

logger = get_logger(__name__)

_ANALYZABLE_STATUSES = {"added", "modified"}


class CodeAnalyzer:
    def __init__(self, config: LocalTestConfig) -> None:
        self._config = config

    def analyze(self, files: list[PRFile]) -> list[AffectedFunction]:
        affected: list[AffectedFunction] = []

        for pr_file in files:
            if pr_file.status not in _ANALYZABLE_STATUSES:
                continue
            functions = self._analyze_file(pr_file)
            affected.extend(functions)
            logger.info(
                "analyzed file",
                extra={"file": pr_file.filename, "functions": len(functions)},
            )

        return affected

    def _analyze_file(self, pr_file: PRFile) -> list[AffectedFunction]:
        source = self._read_source(pr_file.filename)
        if source is None:
            return []

        changed_lines = (
            parse_changed_lines(pr_file.patch)
            if pr_file.patch
            else set(range(1, source.count("\n") + 2))
        )

        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            raise CodeAnalyzerError(
                f"Syntax error in {pr_file.filename}: {exc}"
            ) from exc

        return self._extract_affected(
            tree, source, pr_file.filename, changed_lines
        )

    def _read_source(self, filename: str) -> str | None:
        full_path = os.path.join(self._config.repo_path, filename)
        if not os.path.isfile(full_path):
            logger.warning("file not found", extra={"path": full_path})
            return None
        with open(full_path, encoding="utf-8") as fh:
            return fh.read()

    def _extract_affected(
        self,
        tree: ast.Module,
        source: str,
        file_path: str,
        changed_lines: set[int],
    ) -> list[AffectedFunction]:
        lines = source.splitlines()
        results: list[AffectedFunction] = []

        for node in ast.walk(tree):
            if not isinstance(
                node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
            ):
                continue

            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            if not any(node.lineno <= ln <= end for ln in changed_lines):
                continue

            kind = self._node_kind(node)
            qualified = self._qualified_name(node, tree)
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
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    ) -> str:
        if isinstance(node, ast.ClassDef):
            return "class"
        prefix = "async_" if isinstance(node, ast.AsyncFunctionDef) else ""
        return f"{prefix}function"

    def _qualified_name(
        self,
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
