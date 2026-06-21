"""Pipeline orchestrator for the local automator."""

from __future__ import annotations

from typing import Any, Callable

from pr_test_automator_local._logging import get_logger
from pr_test_automator_local.config import LocalTestConfig
from pr_test_automator_local.llm_bridge import ClaudeCodeBridge, LLMBridge
from pr_test_automator_local.models import (
    GeneratedTest,
    PipelineResult,
    StepOutcome,
    TestRunResult,
)
from pr_test_automator_local.steps import (
    CodeAnalyzer,
    FailureFixer,
    LocalDiffReader,
    TestCommitter,
    TestFinder,
    TestGenerator,
    TestRunner,
)
from pr_test_automator_local.utils.exceptions import LocalTestAutomatorError

logger = get_logger(__name__)

_EMPTY_RUN = TestRunResult(
    passed=0,
    failed=0,
    errors=0,
    total=0,
    output="No tests generated.",
    failed_test_ids=[],
    is_passing=True,
)


class LocalTestPipeline:
    """Orchestrates the local test automation pipeline."""

    def __init__(
        self,
        config: LocalTestConfig,
        llm: LLMBridge | None = None,
    ) -> None:
        self._config = config
        self._llm = llm or ClaudeCodeBridge(
            cmd=config.claude_code_cmd,
            timeout=config.claude_code_timeout,
        )
        self._reader = LocalDiffReader(config)
        self._analyzer = CodeAnalyzer(config)
        self._finder = TestFinder(config)
        self._runner = TestRunner(config)
        self._generator = TestGenerator(config, self._finder, self._llm)
        self._fixer = FailureFixer(config, self._runner, self._llm)
        self._committer = TestCommitter(config)

    def run(self) -> PipelineResult:
        steps: list[StepOutcome] = []
        tests: list[GeneratedTest] = []
        test_result: TestRunResult = _EMPTY_RUN
        commit_sha: str | None = None
        pr_url: str | None = None
        files_changed = 0

        logger.info("pipeline starting", extra={"repo": self._config.repo_path})

        pr_info, step1 = self._step(
            "local_diff_reader", lambda: self._reader.read()
        )
        steps.append(step1)
        if not step1.success or pr_info is None:
            return self._build_result(
                steps, tests, test_result, commit_sha, pr_url, files_changed,
                "", "",
            )
        files_changed = len(pr_info.files)

        if not pr_info.files:
            logger.info("no Python source files changed — done")
            return self._build_result(
                steps, tests, test_result, commit_sha, pr_url, files_changed,
                pr_info.base_branch, pr_info.head_branch,
            )

        affected, step2 = self._step(
            "code_analyzer", lambda: self._analyzer.analyze(pr_info.files)
        )
        steps.append(step2)
        if not affected:
            logger.info("no functions affected — done")
            return self._build_result(
                steps, tests, test_result, commit_sha, pr_url, files_changed,
                pr_info.base_branch, pr_info.head_branch,
            )

        existing_tests, step3 = self._step(
            "test_finder", lambda: self._finder.find(affected)
        )
        steps.append(step3)

        tests, step4 = self._step(
            "test_generator",
            lambda: self._generator.generate(affected, existing_tests or []),
        )
        steps.append(step4)
        if not step4.success or not tests:
            return self._build_result(
                steps, tests, test_result, commit_sha, pr_url, files_changed,
                pr_info.base_branch, pr_info.head_branch,
            )

        test_result, step5 = self._step(
            "test_runner", lambda: self._runner.run(tests)
        )
        steps.append(step5)

        if test_result and not test_result.is_passing:
            fixed, step6 = self._step(
                "failure_fixer",
                lambda: self._fixer.fix(tests, test_result),
            )
            if fixed is not None:
                tests, test_result = fixed
            steps.append(step6)

        commit_result, step7 = self._step(
            "test_committer",
            lambda: self._committer.commit(tests, pr_info, test_result),
        )
        if commit_result is not None:
            commit_sha, pr_url = commit_result
        steps.append(step7)

        logger.info(
            "pipeline complete",
            extra={"is_passing": test_result.is_passing},
        )
        return self._build_result(
            steps, tests, test_result, commit_sha, pr_url, files_changed,
            pr_info.base_branch, pr_info.head_branch,
        )

    def _step(
        self, name: str, fn: Callable[[], Any]
    ) -> tuple[Any, StepOutcome]:
        try:
            result = fn()
            return result, StepOutcome(
                step=name,
                success=True,
                message=f"{name} completed successfully",
            )
        except LocalTestAutomatorError as exc:
            logger.error(f"step {name} failed: {exc}")
            return None, StepOutcome(step=name, success=False, message=str(exc))

    def _build_result(
        self,
        steps: list[StepOutcome],
        tests: list[GeneratedTest],
        test_result: TestRunResult,
        commit_sha: str | None,
        pr_url: str | None,
        files_changed: int,
        base_branch: str,
        head_branch: str,
    ) -> PipelineResult:
        # Overall result is passing only if BOTH conditions hold:
        # 1. Every step that ran completed successfully (no ✗ in the steps
        #    list). This catches cases like test_generator failing with
        #    NotImplementedError for a language whose Stage 4 isn't done.
        # 2. The tests themselves passed (test_result.is_passing).
        #
        # Previously this only checked condition 2, which meant a run that
        # failed at test_generator (so no tests ever ran) would still
        # report PASS because the never-run test result is initialized to
        # is_passing=True. That was misleading and is now fixed.
        all_steps_ok = all(step.success for step in steps)

        return PipelineResult(
            repo_path=self._config.repo_path,
            base_branch=base_branch,
            head_branch=head_branch,
            files_changed=files_changed,
            functions_affected=sum(
                len(t.covered_functions) for t in (tests or [])
            ),
            tests_generated=len(tests or []),
            test_result=test_result,
            commit_sha=commit_sha,
            pr_url=pr_url,
            steps=steps,
            is_passing=all_steps_ok and test_result.is_passing,
        )
