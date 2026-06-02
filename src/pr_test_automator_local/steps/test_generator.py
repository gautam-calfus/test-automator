"""Step 4: Generate pytest tests using the LLM bridge (Claude Code by default)."""

from __future__ import annotations

import re

from pr_test_automator_local._logging import get_logger
from pr_test_automator_local.config import LocalTestConfig
from pr_test_automator_local.llm_bridge import LLMBridge
from pr_test_automator_local.models import (
    AffectedFunction,
    ExistingTest,
    GeneratedTest,
)
from pr_test_automator_local.steps.test_finder import TestFinder
from pr_test_automator_local.utils.diff_parser import extract_code_block
from pr_test_automator_local.utils.exceptions import TestGeneratorError
from pr_test_automator_local.utils.test_parser import (
    TestFunction,
    covers,
    parse_test_functions,
)

logger = get_logger(__name__)

_SYSTEM_PROMPT_FRESH = """\
You are an expert Python test engineer specializing in pytest.
Generate high-quality, production-ready tests following these rules:

- Use pytest with @pytest.mark.unit for unit tests
- For async functions, combine @pytest.mark.asyncio with async def
- Always test: happy path, edge cases (empty/None/boundary values), error cases
- Mock all external dependencies using pytest-mock or unittest.mock
- Name tests as test_{function_name}_{scenario}
- Write descriptive assertion messages
- Import only what is needed
- Do NOT manipulate sys.path; assume the package is installed
- Output ONLY valid Python code, no markdown, no explanation
"""

_SYSTEM_PROMPT_INCREMENTAL = """\
You are an expert Python test engineer specializing in pytest.
You are writing test functions to be ADDED to an existing test module.

CRITICAL — STYLE PRESERVATION:
- Match the EXACT style of the existing tests in the user's prompt
- If existing tests use @pytest.mark.unit, your tests must use it
- If existing tests use `-> None` annotation, your tests must too
- If existing tests omit docstrings, your tests must too
- If existing tests use inline asserts (no `result =` variable), match that
- Mirror the existing naming pattern exactly

Other rules:
- Output ONLY the new test functions (decorators + definitions) — no
  import statements, no module-level code, no markdown, no explanation
- For async functions, use @pytest.mark.asyncio with async def
- Test happy path, edge cases, and error cases for each function
- Mock external dependencies using pytest-mock or unittest.mock
- Name tests as test_{function_name}_{scenario}
- Do not rename existing tests; if replacing a test named X, your new
  test for the same scenario should also be named X
- Separate each test function from the next with TWO blank lines (PEP 8)
"""

_USER_TEMPLATE_FRESH = (
    "Generate pytest tests for the following Python function(s).\n"
    "\n"
    "Source file: {source_file}\n"
    "\n"
    "To import from this file, derive the module path by dropping any 'src/' "
    "prefix and converting slashes to dots, omitting the '.py' extension. "
    "For example, 'src/calculator/discount.py' becomes "
    "'from calculator.discount import ...'.\n"
    "\n"
    "Functions to test:\n"
    "```python\n"
    "{functions_code}\n"
    "```\n"
    "\n"
    "Produce a complete test module with imports and all test functions.\n"
)

_USER_TEMPLATE_INCREMENTAL = (
    "Write pytest test functions to be added to an existing test file.\n"
    "\n"
    "Source file:    {source_file}\n"
    "Test file:      {test_file}\n"
    "\n"
    "Existing test file content (PRESERVE this style):\n"
    "```python\n"
    "{existing_content}\n"
    "```\n"
    "{style_reference_section}"
    "Write tests for ONLY these functions.\n"
    "\n"
    "{functions_section}"
    "\n"
    "Output ONLY the new test function definitions (with their decorators). "
    "Do NOT include imports or other module-level code.\n"
)

_STYLE_REFERENCE_SECTION = (
    "\n"
    "Style reference — the tests being replaced had this style:\n"
    "```python\n"
    "{removed_tests_code}\n"
    "```\n"
)

_FUNCTION_BLOCK = (
    "Function `{name}` (status: {status}):\n"
    "```python\n"
    "{code}\n"
    "```\n"
    "\n"
)


class TestGenerator:
    def __init__(
        self,
        config: LocalTestConfig,
        test_finder: TestFinder,
        llm: LLMBridge,
    ) -> None:
        self._config = config
        self._test_finder = test_finder
        self._llm = llm

    def generate(
        self,
        affected: list[AffectedFunction],
        existing_tests: list[ExistingTest],
    ) -> list[GeneratedTest]:
        by_file = self._group_by_file(affected)
        existing_by_source = {t.source_file_path: t for t in existing_tests}
        results: list[GeneratedTest] = []

        for source_path, functions in by_file.items():
            existing = existing_by_source.get(source_path)
            if existing:
                generated = self._generate_incremental(
                    source_path, functions, existing
                )
            else:
                generated = self._generate_fresh(source_path, functions)
            results.append(generated)
            logger.info(
                "generated tests",
                extra={
                    "source": source_path,
                    "mode": "incremental" if existing else "fresh",
                },
            )
        return results

    def _generate_fresh(
        self,
        source_path: str,
        functions: list[AffectedFunction],
    ) -> GeneratedTest:
        functions_code = "\n\n".join(fn.source_code for fn in functions)
        user_prompt = _USER_TEMPLATE_FRESH.format(
            source_file=source_path,
            functions_code=functions_code,
        )

        try:
            raw = self._llm.generate(_SYSTEM_PROMPT_FRESH, user_prompt)
        except Exception as exc:
            raise TestGeneratorError(
                f"LLM failed for {source_path}: {exc}"
            ) from exc

        code = extract_code_block(raw)
        test_path = self._test_finder.suggest_test_path(source_path, existing=None)

        return GeneratedTest(
            source_file_path=source_path,
            test_file_path=test_path,
            content=code,
            covered_functions=[fn.qualified_name for fn in functions],
        )

    def _generate_incremental(
        self,
        source_path: str,
        functions: list[AffectedFunction],
        existing: ExistingTest,
    ) -> GeneratedTest:
        existing_tests = parse_test_functions(existing.content)

        function_status: list[tuple[AffectedFunction, str, list[TestFunction]]] = []
        tests_to_remove: list[TestFunction] = []

        for fn in functions:
            matching = [t for t in existing_tests if covers(t.name, fn.name)]
            if matching:
                status = "MODIFIED - existing tests will be replaced"
                tests_to_remove.extend(matching)
            else:
                status = "NEW - no existing tests"
            function_status.append((fn, status, matching))

        functions_section = "".join(
            _FUNCTION_BLOCK.format(
                name=fn.name, status=status, code=fn.source_code
            )
            for fn, status, _ in function_status
        )

        removed_tests_code = self._extract_test_source(
            existing.content, tests_to_remove
        )
        style_reference_section = (
            _STYLE_REFERENCE_SECTION.format(
                removed_tests_code=removed_tests_code
            )
            if removed_tests_code.strip()
            else ""
        )

        trimmed_existing = self._remove_tests(existing.content, tests_to_remove)

        user_prompt = _USER_TEMPLATE_INCREMENTAL.format(
            source_file=source_path,
            test_file=existing.test_file_path,
            existing_content=trimmed_existing,
            style_reference_section=style_reference_section,
            functions_section=functions_section,
        )

        try:
            raw = self._llm.generate(
                _SYSTEM_PROMPT_INCREMENTAL, user_prompt
            )
        except Exception as exc:
            raise TestGeneratorError(
                f"LLM failed for {source_path}: {exc}"
            ) from exc

        new_test_code = extract_code_block(raw).strip()
        merged = self._merge(trimmed_existing, new_test_code)

        return GeneratedTest(
            source_file_path=source_path,
            test_file_path=existing.test_file_path,
            content=merged,
            covered_functions=[fn.qualified_name for fn in functions],
        )

    @staticmethod
    def _extract_test_source(
        content: str, tests: list[TestFunction]
    ) -> str:
        if not tests:
            return ""
        lines = content.splitlines(keepends=True)
        blocks: list[str] = []
        for t in tests:
            block = "".join(lines[t.line_start - 1 : t.line_end])
            blocks.append(block.rstrip())
        return "\n\n".join(blocks)

    @staticmethod
    def _remove_tests(content: str, to_remove: list[TestFunction]) -> str:
        if not to_remove:
            return content
        lines = content.splitlines(keepends=True)
        drop: set[int] = set()
        for test in to_remove:
            for i in range(test.line_start - 1, test.line_end):
                drop.add(i)
        kept = [line for i, line in enumerate(lines) if i not in drop]
        return _collapse_blank_runs("".join(kept))

    @staticmethod
    def _merge(existing: str, new_tests: str) -> str:
        if not new_tests:
            return existing
        existing = existing.rstrip() + "\n\n\n"
        normalized = new_tests.strip()
        normalized = re.sub(
            r"\n(?=(@\w|def |async def ))",
            "\n\n\n",
            normalized,
        )
        normalized = re.sub(r"\n{4,}", "\n\n\n", normalized)
        normalized = normalized.lstrip("\n")
        return existing + normalized + "\n"

    @staticmethod
    def _group_by_file(
        affected: list[AffectedFunction],
    ) -> dict[str, list[AffectedFunction]]:
        groups: dict[str, list[AffectedFunction]] = {}
        for fn in affected:
            groups.setdefault(fn.file_path, []).append(fn)
        return groups


def _collapse_blank_runs(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    blank_count = 0
    for line in lines:
        if line.strip() == "":
            blank_count += 1
            if blank_count <= 2:
                out.append(line)
        else:
            blank_count = 0
            out.append(line)
    return "\n".join(out)
