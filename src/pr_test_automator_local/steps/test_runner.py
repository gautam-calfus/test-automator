"""Step 5: Run generated tests and parse results.

Groups generated tests by language, asks each handler to build the runner
command and parse the output. Currently every test file routes to the
PythonLanguageHandler so behavior is identical to the pre-refactor code.
Future languages will route to their own handlers transparently.
"""

from __future__ import annotations

import contextlib
import os
import subprocess

from pr_test_automator_local._logging import get_logger
from pr_test_automator_local.config import LocalTestConfig
from pr_test_automator_local.languages import get_handler_for_file
from pr_test_automator_local.languages.base import LanguageHandler
from pr_test_automator_local.models import GeneratedTest, TestRunResult
from pr_test_automator_local.utils.exceptions import TestRunnerError

logger = get_logger(__name__)

_TIMEOUT_SECONDS = 120  # legacy fallback; config.test_runner_timeout takes precedence


class TestRunner:
    """Writes generated test files, executes them, parses results."""

    def __init__(self, config: LocalTestConfig) -> None:
        self._config = config

    def run(self, tests: list[GeneratedTest]) -> TestRunResult:
        if not tests:
            return TestRunResult(
                passed=0,
                failed=0,
                errors=0,
                total=0,
                output="No tests to run.",
                failed_test_ids=[],
                is_passing=True,
            )

        # Group tests by their language handler so each language's runner
        # is invoked independently with its own subprocess + parser.
        groups = self._group_by_handler(tests)
        outputs: list[str] = []
        passed = failed = errors = 0
        failed_test_ids: list[str] = []
        all_pass = True

        for handler, handler_tests in groups.items():
            result = self._run_for_language(handler, handler_tests)
            outputs.append(
                f"\n=== {handler.name} runner output ===\n{result.output}"
            )
            passed += result.passed
            failed += result.failed
            errors += result.errors
            failed_test_ids.extend(result.failed_test_ids)
            if not result.is_passing:
                all_pass = False

        combined_output = "\n".join(outputs).strip() or "No output."
        logger.info(
            "tests finished",
            extra={"passed": passed, "failed": failed, "errors": errors},
        )

        return TestRunResult(
            passed=passed,
            failed=failed,
            errors=errors,
            total=passed + failed + errors,
            output=combined_output,
            failed_test_ids=failed_test_ids,
            is_passing=all_pass,
        )

    def _run_for_language(
        self,
        handler: LanguageHandler,
        tests: list[GeneratedTest],
    ) -> TestRunResult:
        written: list[str] = []
        try:
            written = self._write_tests(handler, tests)
            if not written:
                return TestRunResult(
                    passed=0,
                    failed=0,
                    errors=0,
                    total=0,
                    output="No new test files written.",
                    failed_test_ids=[],
                    is_passing=True,
                )

            output, return_code = self._run_subprocess(handler, written)
        finally:
            self._cleanup(written)

        try:
            parsed = handler.parse_test_output(output, return_code)
        except NotImplementedError as exc:
            raise TestRunnerError(
                f"Test output parsing for '{handler.name}' is not "
                f"implemented in this release. {exc}"
            ) from exc
        return TestRunResult(
            passed=parsed["passed"],   # type: ignore[arg-type]
            failed=parsed["failed"],   # type: ignore[arg-type]
            errors=parsed["errors"],   # type: ignore[arg-type]
            total=(
                parsed["passed"] + parsed["failed"] + parsed["errors"]  # type: ignore[operator]
            ),
            output=output,
            failed_test_ids=parsed["failed_test_ids"],   # type: ignore[arg-type]
            is_passing=parsed["is_passing"],   # type: ignore[arg-type]
        )

    @staticmethod
    def _group_by_handler(
        tests: list[GeneratedTest],
    ) -> dict[LanguageHandler, list[GeneratedTest]]:
        groups: dict[LanguageHandler, list[GeneratedTest]] = {}
        for gen in tests:
            handler = get_handler_for_file(gen.source_file_path)
            if handler is None:
                logger.warning(
                    "no handler for generated test source — skipping",
                    extra={"source": gen.source_file_path},
                )
                continue
            groups.setdefault(handler, []).append(gen)
        return groups

    def _write_tests(
        self,
        handler: LanguageHandler,
        tests: list[GeneratedTest],
    ) -> list[str]:
        """Write each generated test as a temp file next to where the
        canonical test file would live.

        Putting the temp file in the same directory as the canonical
        ensures the language's test discovery (pytest collection / Gradle
        test source roots) finds it. The old behavior wrote everything to
        a single ``tests/`` directory, which broke Kotlin (Gradle expects
        ``src/test/kotlin/`` layout).

        For some languages the handler may also transform the content for
        the temp-file run (e.g. Kotlin renames the class to match the
        temp file name to avoid duplicate-class compile errors). That
        transformation is delegated to ``handler.transform_for_temp_file``
        if it exists; otherwise the original content is written.
        """
        written: list[str] = []
        transform = getattr(handler, "transform_for_temp_file", None)

        for gen in tests:
            # Temp file lives in the same directory as the canonical
            # test path. That ensures pytest / Gradle find it.
            canonical_abs = os.path.join(
                self._config.repo_path, gen.test_file_path
            )
            target_dir = os.path.dirname(canonical_abs)
            os.makedirs(target_dir, exist_ok=True)

            safe_name = handler.temp_test_file_name(gen.test_file_path)
            dest = os.path.join(target_dir, safe_name)

            if os.path.exists(dest):
                logger.warning(
                    "skipping write — temp file already exists",
                    extra={"path": dest},
                )
                continue

            content = gen.content
            if callable(transform):
                content = transform(content, gen.test_file_path)

            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(content)
            written.append(dest)
        return written

    def _cleanup(self, paths: list[str]) -> None:
        for path in paths:
            with contextlib.suppress(OSError):
                os.remove(path)

    def _run_subprocess(
        self, handler: LanguageHandler, test_files: list[str]
    ) -> tuple[str, int]:
        try:
            cmd = handler.build_test_command(test_files, self._config.repo_path)
        except NotImplementedError as exc:
            raise TestRunnerError(
                f"Test execution for '{handler.name}' is not implemented "
                f"in this release. {exc}"
            ) from exc

        # v0.2.0: timeout configurable via --test-runner-timeout CLI flag.
        # Real Gradle cold starts on large Initech codebases routinely
        # exceed the old 120s default. Default is now 600s (10 min);
        # users can bump higher via the flag.
        runner_timeout = getattr(
            self._config, "test_runner_timeout", _TIMEOUT_SECONDS,
        )

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=self._config.repo_path,
                timeout=runner_timeout,
                check=False,
            )
            combined = proc.stdout + proc.stderr
            if "passed" not in combined and "failed" not in combined:
                logger.warning(
                    "%s runner produced no test summary — output follows:\n%s",
                    handler.name,
                    combined[:2000],
                )
            return combined, proc.returncode
        except subprocess.TimeoutExpired as exc:
            raise TestRunnerError(
                f"{handler.name} runner timed out after {runner_timeout}s"
            ) from exc
        except FileNotFoundError as exc:
            raise TestRunnerError(
                f"{handler.name} runner command not found "
                f"(first arg: {cmd[0]!r}): {exc}"
            ) from exc
