"""Step 5: Run generated tests with pytest, parse results."""

from __future__ import annotations

import contextlib
import os
import re
import subprocess

from pr_test_automator_local._logging import get_logger
from pr_test_automator_local.config import LocalTestConfig
from pr_test_automator_local.models import GeneratedTest, TestRunResult
from pr_test_automator_local.utils.exceptions import TestRunnerError

logger = get_logger(__name__)

_SUMMARY_RE = re.compile(
    r"(?P<count>\d+)\s+(?P<kind>passed|failed|error|errors)",
    re.IGNORECASE,
)
_FAILED_ID_RE = re.compile(r"FAILED\s+(\S+)")
_TIMEOUT_SECONDS = 120
_TEMP_PREFIX = "_pr_automator_"


class TestRunner:
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

        target_dir = self._target_test_dir()
        os.makedirs(target_dir, exist_ok=True)

        written: list[str] = []
        try:
            written = self._write_tests(tests, target_dir)
            output, return_code = self._run_pytest(written)
        finally:
            self._cleanup(written)

        result = self._parse_output(output, return_code)
        logger.info(
            "pytest finished",
            extra={
                "passed": result.passed,
                "failed": result.failed,
                "errors": result.errors,
            },
        )
        return result

    def _target_test_dir(self) -> str:
        first = (
            self._config.test_dirs[0] if self._config.test_dirs else "tests"
        )
        return os.path.join(self._config.repo_path, first)

    def _write_tests(
        self,
        tests: list[GeneratedTest],
        target_dir: str,
    ) -> list[str]:
        written: list[str] = []
        for gen in tests:
            base = os.path.basename(gen.test_file_path)
            safe_name = f"{_TEMP_PREFIX}{base}"
            dest = os.path.join(target_dir, safe_name)

            if os.path.exists(dest):
                logger.warning(
                    "skipping write — temp file already exists",
                    extra={"path": dest},
                )
                continue

            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(gen.content)
            written.append(dest)
        return written

    def _cleanup(self, paths: list[str]) -> None:
        for path in paths:
            with contextlib.suppress(OSError):
                os.remove(path)

    def _run_pytest(self, test_files: list[str]) -> tuple[str, int]:
        cmd = [
            "python",
            "-m",
            "pytest",
            "--tb=short",
            "--no-header",
            "-v",
            "-o", "addopts=",
            "-p", "no:cacheprovider",
            *test_files,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=self._config.repo_path,
                timeout=_TIMEOUT_SECONDS,
                check=False,
            )
            combined = proc.stdout + proc.stderr
            if "passed" not in combined and "failed" not in combined:
                logger.warning(
                    "pytest produced no test summary — output follows:\n%s",
                    combined[:2000],
                )
            return combined, proc.returncode
        except subprocess.TimeoutExpired as exc:
            raise TestRunnerError(
                f"pytest timed out after {_TIMEOUT_SECONDS}s"
            ) from exc
        except FileNotFoundError as exc:
            raise TestRunnerError(
                f"pytest not found — is it installed? {exc}"
            ) from exc

    def _parse_output(self, output: str, return_code: int) -> TestRunResult:
        passed = failed = errors = 0
        for match in _SUMMARY_RE.finditer(output):
            count = int(match.group("count"))
            kind = match.group("kind").lower()
            if kind == "passed":
                passed = count
            elif kind == "failed":
                failed = count
            elif kind in {"error", "errors"}:
                errors = count

        failed_ids = _FAILED_ID_RE.findall(output)

        # Detect "no tests ran / collection failure" cases that don't have a
        # passed/failed/errors summary line at all.
        total_explicit = passed + failed + errors
        no_summary = total_explicit == 0
        has_collection_error = (
            "ImportError" in output
            or "ModuleNotFoundError" in output
            or "no tests ran" in output.lower()
            or "errors during collection" in output.lower()
        )

        if no_summary and (has_collection_error or return_code != 0):
            # Treat as 1 error so the orchestrator surfaces a failure.
            errors = 1

        is_passing = (
            return_code == 0
            and not (no_summary and has_collection_error)
        )

        return TestRunResult(
            passed=passed,
            failed=failed,
            errors=errors,
            total=passed + failed + errors,
            output=output,
            failed_test_ids=failed_ids,
            is_passing=is_passing,
        )