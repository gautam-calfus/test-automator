"""Python (pytest) LLM prompts and test-merge logic.

Extracted from steps/test_generator.py and steps/failure_fixer.py during
the v0.2.0 plugin refactor. The string content of every prompt and the
merge algorithm are unchanged.
"""

from __future__ import annotations

import re

from pr_test_automator_local.models import (
    AffectedFunction,
    ExistingTest,
    GeneratedTest,
)
from pr_test_automator_local.utils.test_parser import (
    TestFunction,
    covers as _ast_covers,
    parse_test_functions,
)

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_FRESH = """\
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

SYSTEM_PROMPT_INCREMENTAL = """\
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

SYSTEM_PROMPT_FIX = """\
You are an expert Python test engineer. A pytest run has produced failures.
Fix the test code so all tests pass.

Rules:
- Output ONLY the corrected test module — no explanation, no markdown fences
- Preserve all passing tests exactly
- Fix imports, mocks, assertions, and async handling as needed
- Do not change the source code being tested
"""

# ---------------------------------------------------------------------------
# User prompt templates
# ---------------------------------------------------------------------------

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

_USER_TEMPLATE_FIX = """\
The following test module produced failures.

Source file: {source_file}

Failing test module:
```python
{test_code}
```

pytest output:
```
{pytest_output}
```

Return the fully corrected test module.
"""


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def user_prompt_fresh(
    source_path: str, affected: list[AffectedFunction]
) -> str:
    """Build the user prompt for generating a brand-new test file."""
    functions_code = "\n\n".join(fn.source_code for fn in affected)
    return _USER_TEMPLATE_FRESH.format(
        source_file=source_path,
        functions_code=functions_code,
    )


def user_prompt_incremental(
    source_path: str,
    existing: ExistingTest,
    affected: list[AffectedFunction],
    trimmed_existing_content: str,
    removed_tests_code: str,
) -> str:
    """Build the user prompt for merging new tests into existing tests."""
    function_status: list[tuple[AffectedFunction, str]] = []
    existing_tests = parse_test_functions(existing.content)

    for fn in affected:
        matching = [t for t in existing_tests if _ast_covers(t.name, fn.name)]
        if matching:
            status = "MODIFIED - existing tests will be replaced"
        else:
            status = "NEW - no existing tests"
        function_status.append((fn, status))

    functions_section = "".join(
        _FUNCTION_BLOCK.format(
            name=fn.name, status=status, code=fn.source_code
        )
        for fn, status in function_status
    )

    style_reference_section = (
        _STYLE_REFERENCE_SECTION.format(removed_tests_code=removed_tests_code)
        if removed_tests_code.strip()
        else ""
    )

    return _USER_TEMPLATE_INCREMENTAL.format(
        source_file=source_path,
        test_file=existing.test_file_path,
        existing_content=trimmed_existing_content,
        style_reference_section=style_reference_section,
        functions_section=functions_section,
    )


def user_prompt_fix(generated: GeneratedTest, pytest_output: str) -> str:
    """Build the user prompt for asking Claude to fix failing tests."""
    return _USER_TEMPLATE_FIX.format(
        source_file=generated.source_file_path,
        test_code=generated.content,
        pytest_output=pytest_output,
    )


# ---------------------------------------------------------------------------
# Test merging — combines trimmed existing with new tests, preserves spacing
# ---------------------------------------------------------------------------


def merge_new_tests(existing: str, new_tests: str) -> str:
    """Append new test functions to existing content with PEP 8 spacing.

    Existing content is rstripped then we add 3 newlines, then the new tests
    have any 1-newline gaps before decorators/functions expanded to the
    canonical 3-newline (two blank lines) gap. Excess >3 newlines are
    collapsed back to 3.
    """
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


# ---------------------------------------------------------------------------
# Existing test parsing & coverage matching
# ---------------------------------------------------------------------------


def parse_existing_test_functions(content: str) -> list[TestFunction]:
    """Wrapper over the shared AST parser; exposed so the orchestrator can
    extract test boundaries without importing utils.test_parser directly.
    """
    return parse_test_functions(content)


def covers(test_name: str, source_function_name: str) -> bool:
    """Whether a test function with this name likely covers the source
    function. Exact match (``test_foo`` covers ``foo``) or prefix
    (``test_foo_zero`` covers ``foo``).
    """
    return _ast_covers(test_name, source_function_name)


# ---------------------------------------------------------------------------
# Helpers for incremental merging: extract/remove specific tests
# ---------------------------------------------------------------------------


def extract_test_source(
    content: str, tests: list[TestFunction]
) -> str:
    """Concatenate the source of the given tests, separated by blank lines.

    Used to build the "style reference" section of the incremental prompt.
    """
    if not tests:
        return ""
    lines = content.splitlines(keepends=True)
    blocks: list[str] = []
    for t in tests:
        block = "".join(lines[t.line_start - 1 : t.line_end])
        blocks.append(block.rstrip())
    return "\n\n".join(blocks)


def remove_tests(content: str, to_remove: list[TestFunction]) -> str:
    """Return the test file content with the listed tests deleted.

    Blank-line runs longer than 2 are collapsed back to 2 so the resulting
    file doesn't accumulate gaps after repeated removals.
    """
    if not to_remove:
        return content
    lines = content.splitlines(keepends=True)
    drop: set[int] = set()
    for test in to_remove:
        for i in range(test.line_start - 1, test.line_end):
            drop.add(i)
    kept = [line for i, line in enumerate(lines) if i not in drop]
    return _collapse_blank_runs("".join(kept))


def _collapse_blank_runs(text: str) -> str:
    """Replace runs of 3+ blank lines with exactly 2 blank lines."""
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
