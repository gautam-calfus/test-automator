"""Step 6: Iteratively fix failing tests using the LLM bridge.

Per-test-file dispatch: each test's source file determines which language
handler's prompts and collection-error markers are used. Skips the fix loop
when the runner reports a collection/compilation error (Claude can't fix
env issues).
"""

from __future__ import annotations

from pr_test_automator_local._logging import get_logger
from pr_test_automator_local.config import LocalTestConfig
from pr_test_automator_local.languages import get_handler_for_file
from pr_test_automator_local.llm_bridge import LLMBridge
from pr_test_automator_local.models import GeneratedTest, TestRunResult
from pr_test_automator_local.steps.test_runner import TestRunner
from pr_test_automator_local.utils.diff_parser import extract_code_block
from pr_test_automator_local.utils.exceptions import FailureFixerError

logger = get_logger(__name__)


class FailureFixer:
    """Asks Claude to fix failing tests, then re-runs."""

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
        if self._is_collection_error(tests, initial_result):
            logger.warning(
                "collection/compilation error detected — skipping fix loop "
                "(install dependencies or check your build setup)"
            )
            return tests, initial_result

        current_tests = list(tests)
        current_result = initial_result

        for attempt in range(1, self._config.max_fix_retries + 1):
            if current_result.is_passing:
                logger.info(
                    "all tests passing",
                    extra={"after_attempt": attempt - 1},
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

    def _is_collection_error(
        self,
        tests: list[GeneratedTest],
        result: TestRunResult,
    ) -> bool:
        """True if any language handler's collection error markers appear
        in the runner output.
        """
        markers: set[str] = set()
        for gen in tests:
            handler = get_handler_for_file(gen.source_file_path)
            if handler is None:
                continue
            markers.update(handler.collection_error_markers())
        return any(marker in result.output for marker in markers)

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
        # Strip the extension to match how failed test IDs look in output
        base = gen.test_file_path.split("/")[-1]
        for ext in (".py", ".java", ".kt", ".js", ".ts", ".tsx"):
            base = base.removesuffix(ext)
        return (
            any(base in tid for tid in result.failed_test_ids)
            or result.errors > 0
        )

    def _fix_one(
        self, gen: GeneratedTest, runner_output: str
    ) -> GeneratedTest:
        handler = get_handler_for_file(gen.source_file_path)
        if handler is None:
            raise FailureFixerError(
                f"No language handler for {gen.source_file_path}; cannot "
                f"build fix prompt."
            )

        try:
            system_prompt = handler.system_prompt_fix()
            user_prompt = handler.user_prompt_fix(gen, runner_output)
        except NotImplementedError as exc:
            raise FailureFixerError(
                f"Fix-loop prompts for '{handler.name}' not implemented "
                f"in this release. {exc}"
            ) from exc

        try:
            raw = self._llm.generate(system_prompt, user_prompt)
        except Exception as exc:
            raise FailureFixerError(
                f"LLM failed while fixing {gen.test_file_path}: {exc}"
            ) from exc

        # Use the handler's own extractor (Python markdown, Kotlin
        # source extractor, etc.) instead of the generic Python-flavored
        # extract_code_block. This is critical for non-Python languages
        # where the LLM response may contain prose, markdown fences, or
        # agent narration that the generic extractor can't strip.
        # v0.2.0a6.post4 fix.
        extract_hook = getattr(handler, "extract_code", None)
        if extract_hook is not None:
            try:
                fixed_code = extract_hook(raw, mode="fix")
            except Exception as exc:
                # If extraction fails for the fix response (e.g. Claude
                # returned pure prose), surface that as a clear error
                # rather than writing garbage to disk.
                raise FailureFixerError(
                    f"Could not extract clean source from LLM fix "
                    f"response for {gen.test_file_path}: {exc}"
                ) from exc
        else:
            # Fallback to the Python-markdown extractor for handlers
            # that don't expose an extract_code hook
            fixed_code = extract_code_block(raw)

        return gen.model_copy(update={"content": fixed_code})
