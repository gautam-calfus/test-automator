"""Step 6: Iteratively fix failing tests using the LLM bridge."""

from __future__ import annotations

from pr_test_automator_local._logging import get_logger
from pr_test_automator_local.config import LocalTestConfig
from pr_test_automator_local.llm_bridge import LLMBridge
from pr_test_automator_local.models import GeneratedTest, TestRunResult
from pr_test_automator_local.steps.test_runner import TestRunner
from pr_test_automator_local.utils.diff_parser import extract_code_block
from pr_test_automator_local.utils.exceptions import FailureFixerError

logger = get_logger(__name__)

_SYSTEM_PROMPT = """\
You are an expert Python test engineer. A pytest run has produced failures.
Fix the test code so all tests pass.

Rules:
- Output ONLY the corrected test module — no explanation, no markdown fences
- Preserve all passing tests exactly
- Fix imports, mocks, assertions, and async handling as needed
- Do not change the source code being tested
"""

_USER_TEMPLATE = """\
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


class FailureFixer:
    def __init__(
        self,
        config: LocalTestConfig,
        runner: TestRunner,
        llm: LLMBridge,
    ) -> None:
        self._config = config
        self._runner = runner
        self._llm = llm

    def fix(
        self,
        tests: list[GeneratedTest],
        initial_result: TestRunResult,
    ) -> tuple[list[GeneratedTest], TestRunResult]:
        # Skip on collection errors — fix loop can't recover from those.
        if (
            "ImportError" in initial_result.output
            or "ModuleNotFoundError" in initial_result.output
        ):
            logger.warning(
                "import error detected — skipping fix loop "
                "(install your project as a package first)"
            )
            return tests, initial_result

        current_tests = list(tests)
        current_result = initial_result

        for attempt in range(1, self._config.max_fix_retries + 1):
            if current_result.is_passing:
                logger.info(
                    "all tests passing", extra={"after_attempt": attempt - 1}
                )
                break

            logger.info(
                "fix attempt",
                extra={
                    "attempt": attempt,
                    "max": self._config.max_fix_retries,
                    "failed": current_result.failed,
                    "errors": current_result.errors,
                },
            )

            current_tests = self._fix_round(current_tests, current_result)
            current_result = self._runner.run(current_tests)

        if not current_result.is_passing:
            logger.warning(
                "tests still failing after fix attempts",
                extra={"max": self._config.max_fix_retries},
            )

        return current_tests, current_result

    def _fix_round(
        self,
        tests: list[GeneratedTest],
        result: TestRunResult,
    ) -> list[GeneratedTest]:
        return [
            self._fix_one(gen, result.output)
            if self._has_failures(gen, result)
            else gen
            for gen in tests
        ]

    @staticmethod
    def _has_failures(gen: GeneratedTest, result: TestRunResult) -> bool:
        base = gen.test_file_path.split("/")[-1].replace(".py", "")
        return (
            any(base in tid for tid in result.failed_test_ids)
            or result.errors > 0
        )

    def _fix_one(self, gen: GeneratedTest, pytest_output: str) -> GeneratedTest:
        prompt = _USER_TEMPLATE.format(
            source_file=gen.source_file_path,
            test_code=gen.content,
            pytest_output=pytest_output,
        )

        try:
            raw = self._llm.generate(_SYSTEM_PROMPT, prompt)
        except Exception as exc:
            raise FailureFixerError(
                f"LLM failed while fixing {gen.test_file_path}: {exc}"
            ) from exc

        fixed_code = extract_code_block(raw)
        return gen.model_copy(update={"content": fixed_code})
