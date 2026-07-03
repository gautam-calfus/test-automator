"""Kotlin (Gradle + JUnit 5) test runner.

Stage 3 of the v0.2.0 rollout. Builds the ``./gradlew`` invocation and
parses Gradle's stdout — specifically the format produced by the
``com.adarshr.test-logger`` plugin that Initech's accounts-service uses.

This parser was written against real Gradle output captured from
accounts-service for three scenarios: passing tests, failing tests, and
a Kotlin compile error. See ``tests/test_kotlin_runner.py`` for the exact
fixtures.

Output format reference (test-logger plugin enabled):

    Passing run:
        > Task :test
        unit.services.JwtAuthServiceTests
          Test some test name() PASSED
        SUCCESS: Executed 1 tests in 1.6s
        BUILD SUCCESSFUL in 6s

    Failing run:
        > Task :test FAILED
        unit.services.JwtAuthServiceTests
          Test some test name() FAILED
          ▼ Expect that "x":
            ✗ is equal to "y"
              at unit.services.X.test name(File.kt:30)
        FAILURE: Executed 1 tests in 1s (1 failed)
        BUILD FAILED in 5s

    Compile error:
        > Task :compileTestKotlin
        e: /path/File.kt: (13, 1): Expecting member declaration
        > Task :compileTestKotlin FAILED
        BUILD FAILED in 15s
        (Note: > Task :test does NOT appear)

Critical distinguishing signal: if ``> Task :test`` appeared, tests ran
(though some may have failed). If only ``:compileTestKotlin FAILED``
appeared, tests never ran — that's a compile error, not a test failure.
"""

from __future__ import annotations

import os
import re

# ---------------------------------------------------------------------------
# Build the gradle command
# ---------------------------------------------------------------------------


def build_test_command(test_files: list[str], repo_path: str) -> list[str]:
    """Construct the argv list to invoke Gradle for the given test files.

    Each test file path is converted to a fully-qualified class name using
    the standard Gradle test layout (src/test/kotlin/<pkg>/<Class>.kt).
    Gradle's ``--tests`` flag accepts multiple values; we pass one per
    test file so each generated test gets its own class filter.

    ``--console=plain`` is critical: without it, the ``test-logger``
    plugin uses an interactive output mode with progress indicators and
    the structured format this module parses isn't produced. With it, we
    get the deterministic format shown in this module's docstring.

    The ``./gradlew`` wrapper is preferred over a system-wide ``gradle``
    because Initech pins a specific Gradle version in
    ``gradle/wrapper/gradle-wrapper.properties``. We do not pass
    ``--no-daemon`` because the daemon makes repeated runs much faster
    (important for the fix loop) and we don't expect to run in CI from
    this tool.
    """
    cmd = ["./gradlew", "test", "--console=plain"]
    for path in test_files:
        class_name = _path_to_class_name(path)
        cmd.extend(["--tests", class_name])
    return cmd


def _path_to_class_name(test_file_path: str) -> str:
    """Convert ``src/test/kotlin/unit/services/FooTests.kt`` →
    ``unit.services.FooTests``.

    Strips the conventional ``src/test/kotlin/`` prefix and the ``.kt``
    extension, replacing path separators with dots. If the path doesn't
    have the conventional prefix, we strip from the rightmost
    ``src/test/kotlin/`` if present, or fall back to the basename without
    extension.
    """
    # Normalize separators
    norm = test_file_path.replace(os.sep, "/")

    # Strip the conventional src/test/kotlin/ prefix
    prefix = "src/test/kotlin/"
    idx = norm.rfind(prefix)
    if idx >= 0:
        norm = norm[idx + len(prefix) :]

    # Drop .kt extension
    if norm.endswith(".kt"):
        norm = norm[:-3]

    # Path → package.Class
    return norm.replace("/", ".")


# ---------------------------------------------------------------------------
# Parse Gradle output
# ---------------------------------------------------------------------------


# Matches ``SUCCESS: Executed N tests in Xs``
# Captures N as group 1.
_SUCCESS_SUMMARY_RE = re.compile(
    r"^SUCCESS:\s+Executed\s+(\d+)\s+tests?",
    re.MULTILINE,
)

# Matches ``FAILURE: Executed N tests in Xs (M failed)``
# Captures N as group 1, M as group 2.
_FAILURE_SUMMARY_RE = re.compile(
    r"^FAILURE:\s+Executed\s+(\d+)\s+tests?\s+in\s+\S+\s+\((\d+)\s+failed\)",
    re.MULTILINE,
)

# Matches per-test FAILED line:
#   "  Test some test name() FAILED"
# We capture the test name (without surrounding parens) for the
# failed_test_ids list.
_FAILED_TEST_LINE_RE = re.compile(
    r"^\s+Test\s+(.+?)\(\)\s+FAILED",
    re.MULTILINE,
)

# Matches per-test PASSED line — used in defensive parsing when the
# summary line is missing for some reason (it's appeared once in our
# fixtures, but defensive).
_PASSED_TEST_LINE_RE = re.compile(
    r"^\s+Test\s+(.+?)\(\)\s+PASSED",
    re.MULTILINE,
)

# Markers signalling a compile error rather than a test failure. If any
# of these appear in output, tests never ran. The TestRunner uses the
# handler's ``collection_error_markers()`` to bail the fix loop in this
# case (no point asking Claude to "fix" the test when the issue is in
# the user's project setup).
COMPILE_ERROR_MARKERS: tuple[str, ...] = (
    "Task :compileKotlin FAILED",
    "Task :compileTestKotlin FAILED",
    "Task :compileJava FAILED",
    "Task :compileTestJava FAILED",
    "Compilation error",
    "BUILD FAILED",  # weak signal alone, but combined with others tells us
)

# Stronger compile error markers — these alone justify "we couldn't run
# tests, this is a compile/build problem."
_STRONG_COMPILE_ERROR_MARKERS = (
    "Task :compileKotlin FAILED",
    "Task :compileTestKotlin FAILED",
    "Task :compileJava FAILED",
    "Task :compileTestJava FAILED",
    "Compilation error",
)


def parse_test_output(
    output: str, return_code: int
) -> dict[str, int | bool | list[str]]:
    """Convert Gradle's stdout into structured counts.

    Returns a dict with keys ``passed``, ``failed``, ``errors``,
    ``failed_test_ids`` (list[str]), and ``is_passing`` (bool).

    Three scenarios this handles:

    1. ``SUCCESS: Executed N tests`` present → passed=N, failed=0.
    2. ``FAILURE: Executed N tests in Xs (M failed)`` present → passed=N-M,
       failed=M. Per-test FAILED lines populate failed_test_ids.
    3. Neither summary line present + ``> Task :compileTestKotlin FAILED``
       (or similar) → errors=1, no per-test info available.

    is_passing is True iff return_code == 0 AND we saw a SUCCESS summary
    line. A non-zero return_code with no SUCCESS line is always treated
    as failing.
    """
    passed = 0
    failed = 0
    errors = 0
    failed_test_ids: list[str] = []

    success_match = _SUCCESS_SUMMARY_RE.search(output)
    failure_match = _FAILURE_SUMMARY_RE.search(output)

    if failure_match:
        total = int(failure_match.group(1))
        failed = int(failure_match.group(2))
        passed = max(total - failed, 0)
        failed_test_ids = _FAILED_TEST_LINE_RE.findall(output)
    elif success_match:
        passed = int(success_match.group(1))
    else:
        # Neither summary line — likely a compile error or some other
        # build problem before tests could run. Count it as one error
        # so the orchestrator surfaces a failure.
        if _is_compile_error(output) or return_code != 0:
            errors = 1
        # If return_code is 0 with no summary, treat as zero tests run
        # (probably the test class name didn't match anything).

    is_passing = (
        return_code == 0
        and success_match is not None
        and failure_match is None
    )

    return {
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "failed_test_ids": failed_test_ids,
        "is_passing": is_passing,
    }


def _is_compile_error(output: str) -> bool:
    """True if Gradle output looks like a compile/build error rather than
    a test failure.
    """
    return any(marker in output for marker in _STRONG_COMPILE_ERROR_MARKERS)


def collection_error_markers() -> tuple[str, ...]:
    """Substrings in Gradle output that signal a BUILD ENVIRONMENT
    error (NOT a compile error in the test code).

    Why this matters: the failure_fixer uses this to decide whether to
    even attempt a fix loop. We only want to bail when the error is
    something Claude CANNOT fix by rewriting test code — like missing
    Maven dependencies, daemon lock issues, or JVM startup failures.

    Compile errors (unresolved reference, type mismatch, wrong number
    of arguments, etc.) are deliberately NOT included here, because
    those are often fixable by rewriting the test. The fix loop should
    get a chance to try. If Claude can't fix it after max_fix_retries
    attempts, the run fails — that's better than silently giving up
    on a fixable problem.

    History: in v0.2.0a6.post3 and earlier, this returned compile
    errors too, which caused the bot to skip the fix loop on every
    compile error including ones Claude could easily fix (positional
    vs named argument mistakes, missing imports, etc.). Changed in
    v0.2.0a6.post4.
    """
    return (
        # Dependency resolution failures — happen before compile, Claude
        # can't fix by rewriting test code
        "Could not resolve all files for configuration",
        "Could not find or load main class",
        # Build infrastructure issues
        "Could not create service of type FileAccessTimeJournal",
        "Timeout waiting to lock journal cache",
        # Gradle init failures
        "Could not start your build",
    )
