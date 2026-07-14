"""Regression tests for FailureFixer._has_failures.

The bug this pins down: failed-test-id formats differ by framework.
pytest ids embed the file path, but Jest ids are bare test titles and
Kotlin's are backticked English names — neither contains the test
file's basename. The original implementation matched only on basename,
so for Jest/Kotlin assertion failures the fixer attributed nothing,
skipped every file, and looped max_fix_retries times without a single
LLM call (observed live on the node-demo-app first run: three "fix
attempts" in two seconds, identical 28/13 counts each time).
"""

from __future__ import annotations

from test_automator.models import GeneratedTest, TestRunResult
from test_automator.steps.failure_fixer import FailureFixer


def _gen(test_path: str, source_path: str, covered: list[str]) -> GeneratedTest:
    return GeneratedTest(
        source_file_path=source_path,
        test_file_path=test_path,
        content="// test content",
        covered_functions=covered,
    )


def _result(failed_ids: list[str], errors: int = 0) -> TestRunResult:
    return TestRunResult(
        passed=1,
        failed=len(failed_ids),
        errors=errors,
        total=1 + len(failed_ids) + errors,
        output="",
        failed_test_ids=failed_ids,
        is_passing=False,
    )


def test_pytest_style_ids_match_by_file_name() -> None:
    gen = _gen("tests/test_format.py", "src/format.py", ["percentage_of"])
    result = _result(["tests/test_format.py::test_percentage_of_zero"])
    assert FailureFixer._has_failures(gen, result) is True


def test_jest_style_ids_match_by_covered_function() -> None:
    """Jest fullNames are 'describe-title test-title' — no file path."""
    gen = _gen(
        "src/utils/discount.test.js",
        "src/utils/discount.js",
        ["tierFor", "applyDiscount"],
    )
    result = _result(
        ["tierFor tierFor returns 0 when total is below the lowest tier"]
    )
    assert FailureFixer._has_failures(gen, result) is True


def test_kotlin_style_ids_match_by_covered_function() -> None:
    """Kotlin failed ids are the backticked English names."""
    gen = _gen(
        "src/test/kotlin/unit/ExtensionsTests.kt",
        "src/main/kotlin/com/x/Extensions.kt",
        ["percentageOf"],
    )
    result = _result(["percentageOf() returns 0 when receiver is 0"])
    assert FailureFixer._has_failures(gen, result) is True


def test_unrelated_failures_are_not_attributed() -> None:
    """Failures from another file's functions must not pull this file
    into the fix round — that's a wasted LLM call per retry.
    """
    gen = _gen(
        "src/utils/discount.test.js",
        "src/utils/discount.js",
        ["tierFor", "applyDiscount"],
    )
    result = _result(["percentageOf percentageOf clamps to 100"])
    assert FailureFixer._has_failures(gen, result) is False


def test_function_name_match_is_word_bounded() -> None:
    """'percentageOf' must not match 'percentageOfTotal' — camelCase
    superstrings are different functions.
    """
    gen = _gen(
        "src/utils/format.test.js", "src/utils/format.js", ["percentageOf"]
    )
    result = _result(["percentageOfTotal handles empty carts"])
    assert FailureFixer._has_failures(gen, result) is False


def test_errors_always_attribute() -> None:
    gen = _gen("src/utils/a.test.js", "src/utils/a.js", ["fn"])
    result = _result([], errors=1)
    assert FailureFixer._has_failures(gen, result) is True


# --- single-file fix always attempts, regardless of attribution ---

def test_single_file_failure_triggers_llm_even_without_name_match():
    """The per-file flow hands the fixer ONE file. Its failure must be
    fixed even when the failed-test id doesn't contain the file/class or
    a covered-function name (the common Kotlin backtick-name case) —
    otherwise retries are silent no-ops."""
    from types import SimpleNamespace

    gen = _gen(
        "src/test/kotlin/unit/services/FooServiceTests.kt",
        "src/main/kotlin/com/x/FooService.kt",
        ["logRevisitIfNavigatedBack"],
    )
    failing = _result(["does not log when already visited"])
    passing = TestRunResult(
        passed=25, failed=0, errors=0, total=25, output="",
        failed_test_ids=[], is_passing=True,
    )

    class _Runner:
        def run(self, tests):
            return passing

    class _LLM:
        def __init__(self):
            self.calls = 0

        def generate(self, system_prompt, user_prompt):
            self.calls += 1
            return (
                "package unit.services\n\n"
                "class FooServiceTests {\n"
                "    @Test\n    fun `x`() {}\n}\n"
            )

    llm = _LLM()
    fixer = FailureFixer(SimpleNamespace(max_fix_retries=3), _Runner(), llm)
    tests, result = fixer.fix([gen], failing)

    assert llm.calls >= 1, "single-file failure must invoke the LLM fixer"
    assert result.is_passing
