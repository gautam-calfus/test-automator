"""Pipeline orchestrator for the local automator."""

from __future__ import annotations

from typing import Any, Callable

from test_automator._logging import get_logger
from test_automator.config import LocalTestConfig
from test_automator.llm_bridge import LLMBridge, create_bridge
from test_automator.models import (
    AffectedFunction,
    GeneratedTest,
    PipelineResult,
    StepOutcome,
    TestRunResult,
)
from test_automator.steps import (
    CodeAnalyzer,
    FailureFixer,
    LocalDiffReader,
    TestCommitter,
    TestFinder,
    TestGenerator,
    TestRunner,
)
from test_automator.utils.exceptions import LocalTestAutomatorError

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
        # --llm-cmd overrides the binary for any provider; for the
        # default claude provider, fall back to the (older) dedicated
        # --claude-code-cmd flag so existing setups keep working.
        cmd_override = config.llm_cmd
        if cmd_override is None and config.llm_provider == "claude":
            cmd_override = config.claude_code_cmd
        self._llm = llm or create_bridge(
            provider=config.llm_provider,
            cmd=cmd_override,
            timeout=config.claude_code_timeout,
            max_output_tokens=config.claude_code_max_output_tokens,
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
            logger.info(
                "no eligible source files changed — done. "
                "Check that your --source-root path matches the actual "
                "directory case (e.g. 'src/main/kotlin', not "
                "'src/main/Kotlin') and that the file extensions are "
                "supported. Both committed and uncommitted working-tree "
                "changes are analyzed by default; with --committed-only, "
                "only COMMITTED changes are visible."
            )
            return self._build_result(
                steps, tests, test_result, commit_sha, pr_url, files_changed,
                pr_info.base_branch, pr_info.head_branch,
            )

        analysis, step2 = self._step(
            "code_analyzer",
            lambda: (
                self._analyzer.analyze(pr_info.files),
                # v0.2: functions deleted since the merge-base. Their
                # existing tests reference dead symbols and get pruned.
                self._analyzer.find_removed(pr_info.files),
            ),
        )
        steps.append(step2)
        affected, removed = analysis if analysis else ([], [])
        if not affected and not removed:
            logger.info("no functions affected — done")
            return self._build_result(
                steps, tests, test_result, commit_sha, pr_url, files_changed,
                pr_info.base_branch, pr_info.head_branch,
            )

        # v0.3.0a6: apply --file whitelist (if set) FIRST. When the user
        # names specific files, that trumps all other filtering.
        # Both filters only look at .file_path, so they apply to removed
        # functions the same way they apply to affected ones.
        affected = self._apply_file_whitelist(affected)
        removed = self._apply_file_whitelist(removed)
        if not affected and not removed:
            logger.info(
                "no functions remain after --file whitelist — done"
            )
            return self._build_result(
                steps, tests, test_result, commit_sha, pr_url, files_changed,
                pr_info.base_branch, pr_info.head_branch,
            )

        # v0.3.0a5: apply --java-file-filter to skip categories that
        # aren't worth generating tests for (e.g. controllers/daos when
        # the user just wants services). This runs AFTER the analyzer
        # so the diff summary remains complete and honest, but BEFORE
        # test generation so we don't waste LLM calls.
        affected = self._apply_java_file_filter(affected)
        removed = self._apply_java_file_filter(removed)
        if not affected and not removed:
            logger.info(
                "no functions remain after --java-file-filter — done"
            )
            return self._build_result(
                steps, tests, test_result, commit_sha, pr_url, files_changed,
                pr_info.base_branch, pr_info.head_branch,
            )

        existing_tests, step3 = self._step(
            # find() only reads .file_path, so removed functions ride
            # along — their source files need existing-test lookup too.
            "test_finder", lambda: self._finder.find(affected + removed)
        )
        steps.append(step3)

        # v0.2: generation, running, and fixing are interleaved PER
        # FILE — each generated file's tests run (and get fixed)
        # immediately, before the next file is generated. Feedback is
        # instant and failures are attributed to their file by
        # construction, so the fixer never burns LLM calls on files
        # that already pass.
        gen_out, step4 = self._step(
            "test_generator",
            lambda: self._generate_run_fix(
                affected, existing_tests or [], removed
            ),
        )
        steps.append(step4)
        tests, solo_result = gen_out if gen_out else ([], None)
        if not step4.success or not tests:
            return self._build_result(
                steps, tests, test_result, commit_sha, pr_url, files_changed,
                pr_info.base_branch, pr_info.head_branch,
            )

        # Final combined run: every generated file together, as the
        # committer would write them. This is the commit gate. With a
        # single file it would just repeat the per-file run, so its
        # result is reused.
        test_result, step5 = self._step(
            "test_runner",
            lambda: (
                solo_result
                if solo_result is not None and len(tests) == 1
                else self._runner.run(tests)
            ),
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
            extra={
                # v0.2.0 fix: test_result is None when test_runner failed
                # (e.g. Gradle timeout). Treat the pipeline as failing in
                # that case rather than crashing on a NoneType attribute
                # access.
                "is_passing": (
                    test_result.is_passing if test_result is not None else False
                ),
            },
        )
        return self._build_result(
            steps, tests, test_result, commit_sha, pr_url, files_changed,
            pr_info.base_branch, pr_info.head_branch,
        )

    def _generate_run_fix(
        self,
        affected: list[AffectedFunction],
        existing_tests: list,
        removed: list,
    ) -> tuple[list[GeneratedTest], TestRunResult | None]:
        """Generate each file's tests, then run and fix them BEFORE
        generating the next file.

        Compared to the old generate-everything-then-run-once flow:
        - a compile error is isolated to the file that caused it (the
          other files aren't even on disk yet);
        - the fix loop gets a single file's failures in its prompt and
          spends LLM calls only on files that actually fail;
        - a file that can't be fixed doesn't poison judgment of the
          rest.

        Returns ``(tests, solo_result)`` — ``solo_result`` is the last
        per-file run result when exactly one file was produced, so the
        orchestrator can skip a redundant combined run.
        """
        tests: list[GeneratedTest] = []
        last_result: TestRunResult | None = None

        for gen in self._generator.iter_generate(
            affected, existing_tests, removed
        ):
            result = self._runner.run([gen])
            logger.info(
                "per-file run | file=%s passed=%s failed=%s errors=%s",
                gen.test_file_path,
                result.passed,
                result.failed,
                result.errors,
            )
            if not result.is_passing:
                fixed_tests, result = self._fixer.fix([gen], result)
                if fixed_tests:
                    gen = fixed_tests[0]
            tests.append(gen)
            last_result = result

        return tests, (last_result if len(tests) == 1 else None)

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

    def _apply_file_whitelist(self, affected: list) -> list:
        """When --file is passed one or more times, drop everything else.

        Works on any items exposing ``.file_path`` (AffectedFunction,
        RemovedFunction).

        Path comparison normalizes both sides so ``./src/main/java/Foo.java``,
        ``src/main/java/Foo.java``, and ``src\\main\\java\\Foo.java`` all
        match. Anything outside the whitelist is dropped with a summary
        log line so the user knows what got skipped.
        """
        whitelist = getattr(self._config, "file_whitelist", None)
        if not whitelist:
            return affected

        normalized_whitelist = {
            self._normalize_path(p) for p in whitelist
        }

        kept: list[AffectedFunction] = []
        skipped: list[str] = []
        for fn in affected:
            if self._normalize_path(fn.file_path) in normalized_whitelist:
                kept.append(fn)
            else:
                if fn.file_path not in skipped:
                    skipped.append(fn.file_path)

        if skipped:
            logger.info(
                "file whitelist dropped %d file(s) not in --file list. "
                "Kept: %s. Skipped: %s",
                len(skipped),
                ", ".join(whitelist),
                ", ".join(skipped[:10]) + (
                    f" ... and {len(skipped) - 10} more"
                    if len(skipped) > 10 else ""
                ),
            )
        return kept

    @staticmethod
    def _normalize_path(p: str) -> str:
        """Normalize a path for whitelist comparison. Handles ``./``
        prefix, backslash separators, and trailing slashes.
        """
        p = p.replace("\\", "/")
        if p.startswith("./"):
            p = p[2:]
        return p.rstrip("/")

    def _apply_java_file_filter(self, affected: list) -> list:
        """Filter affected functions by Java file category.

        Works on any items exposing ``.file_path`` (AffectedFunction,
        RemovedFunction).

        The filter is Java-specific. Python/Kotlin functions pass
        through unchanged. Java files matching a category the user
        did NOT request are dropped, and a summary log line records
        what was skipped so the user knows why their diff didn't
        produce as many tests as expected.

        Returns the filtered list. If no filter is configured, returns
        the input list unchanged.
        """
        file_filter = getattr(self._config, "java_file_filter", None)
        if not file_filter:
            return affected

        from test_automator.languages.java.file_filter import (
            classify_java_file, should_process_java_file,
        )

        kept: list[AffectedFunction] = []
        # Track why each file was dropped, for the summary log
        skipped: dict[str, str] = {}
        for fn in affected:
            if not fn.file_path.endswith(".java"):
                # Non-Java files always pass through
                kept.append(fn)
                continue
            if should_process_java_file(fn.file_path, file_filter):
                kept.append(fn)
            else:
                category = classify_java_file(fn.file_path) or "unknown"
                skipped[fn.file_path] = category

        if skipped:
            logger.info(
                "java file filter dropped %d file(s) not matching "
                "categories %s. Skipped: %s",
                len(skipped),
                ", ".join(file_filter),
                ", ".join(
                    f"{path} ({cat})" for path, cat in skipped.items()
                ),
            )
        return kept

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
            is_passing=(
                all_steps_ok
                and test_result is not None
                and test_result.is_passing
            ),
        )
