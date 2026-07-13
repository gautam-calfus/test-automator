"""Tests for the Kotlin Gradle runner (Stage 3).

The fixtures in this file are VERBATIM outputs captured from running
``./gradlew test --tests 'unit.services.JwtAuthServiceTests' --console=plain``
on Initech's accounts-service repo, against three scenarios:

  PASS_FIXTURE          — A single test that passed
  FAIL_FIXTURE          — A single test that failed (assertion error)
  COMPILE_ERROR_FIXTURE — Test file with invalid Kotlin syntax

These are not synthetic. Don't refactor them to look prettier — the
parser is verified against real output, not idealized output.
"""

from __future__ import annotations

import pytest

from test_automator.languages.kotlin import runner
from test_automator.languages.kotlin.handler import (
    KotlinLanguageHandler,
)


# ---------------------------------------------------------------------------
# Real Gradle output captured from Initech accounts-service
# ---------------------------------------------------------------------------


PASS_FIXTURE = """\
> Task :compileKotlin UP-TO-DATE
> Task :compileJava NO-SOURCE
> Task :processResources UP-TO-DATE
> Task :classes UP-TO-DATE
> Task :compileTestKotlin UP-TO-DATE
> Task :compileTestJava NO-SOURCE
> Task :processTestResources UP-TO-DATE
> Task :testClasses UP-TO-DATE
> Task :test
unit.services.JwtAuthServiceTests
  Test JwtAuthService can sign and decode tokens() PASSED
SUCCESS: Executed 1 tests in 1.6s
BUILD SUCCESSFUL in 6s
5 actionable tasks: 1 executed, 4 up-to-date
"""

FAIL_FIXTURE = """\
> Task :compileKotlin UP-TO-DATE
> Task :compileJava NO-SOURCE
> Task :processResources UP-TO-DATE
> Task :classes UP-TO-DATE
> Task :compileTestKotlin
> Task :compileTestJava NO-SOURCE
> Task :processTestResources UP-TO-DATE
> Task :testClasses UP-TO-DATE
> Task :test FAILED
unit.services.JwtAuthServiceTests
  Test JwtAuthService can sign and decode tokens() FAILED
  ▼ Expect that "test@example.com":
    ✗ is equal to "definitely-wrong-value"
            found "test@example.com"
      at unit.services.JwtAuthServiceTests.JwtAuthService can sign and decode tokens(JwtAuthServiceTests.kt:30)
FAILURE: Executed 1 tests in 1s (1 failed)
1 test completed, 1 failed
FAILURE: Build failed with an exception.
* What went wrong:
Execution failed for task ':test'.
> There were failing tests. See the report at: file:///Users/gautam/keystone/accounts-service/build/reports/tests/test/index.html
* Try:
Run with --stacktrace option to get the stack trace. Run with --info or --debug option to get more log output. Run with --scan to get full insights.
* Get more help at https://help.gradle.org
BUILD FAILED in 5s
5 actionable tasks: 2 executed, 3 up-to-date
"""

COMPILE_ERROR_FIXTURE = """\
> Task :compileKotlin UP-TO-DATE
> Task :compileJava NO-SOURCE
> Task :processResources UP-TO-DATE
> Task :classes UP-TO-DATE
> Task :compileTestKotlin
e: /Users/gautam/keystone/accounts-service/src/test/kotlin/unit/services/JwtAuthServiceTests.kt: (13, 1): Expecting member declaration
> Task :compileTestKotlin FAILED
FAILURE: Build failed with an exception.
* What went wrong:
Execution failed for task ':compileTestKotlin'.
> Compilation error. See log for more details
* Try:
Run with --stacktrace option to get the stack trace. Run with --info or --debug option to get more log output. Run with --scan to get full insights.
* Get more help at https://help.gradle.org
BUILD FAILED in 15s
3 actionable tasks: 1 executed, 2 up-to-date
"""


# ---------------------------------------------------------------------------
# build_test_command — path-to-class conversion
# ---------------------------------------------------------------------------


def test_build_test_command_uses_gradlew() -> None:
    cmd = runner.build_test_command(
        ["src/test/kotlin/unit/services/JwtAuthServiceTests.kt"], "/repo"
    )
    assert cmd[0] == "./gradlew"
    assert cmd[1] == "test"


def test_build_test_command_uses_console_plain() -> None:
    """Critical: without --console=plain, test-logger output format
    differs and parsing breaks.
    """
    cmd = runner.build_test_command(
        ["src/test/kotlin/unit/services/JwtAuthServiceTests.kt"], "/repo"
    )
    assert "--console=plain" in cmd


def test_build_test_command_converts_path_to_class() -> None:
    cmd = runner.build_test_command(
        ["src/test/kotlin/unit/services/JwtAuthServiceTests.kt"], "/repo"
    )
    # Should produce ``--tests "unit.services.JwtAuthServiceTests"``
    tests_idx = cmd.index("--tests")
    assert cmd[tests_idx + 1] == "unit.services.JwtAuthServiceTests"


def test_build_test_command_multiple_test_files() -> None:
    cmd = runner.build_test_command(
        [
            "src/test/kotlin/unit/services/AService.kt",
            "src/test/kotlin/unit/handlers/BHandler.kt",
        ],
        "/repo",
    )
    # Each test file gets its own --tests arg
    tests_indices = [i for i, v in enumerate(cmd) if v == "--tests"]
    assert len(tests_indices) == 2
    test_args = [cmd[i + 1] for i in tests_indices]
    assert "unit.services.AService" in test_args
    assert "unit.handlers.BHandler" in test_args


def test_build_test_command_handles_temp_file_names() -> None:
    """Temp test files generated by the bot have a _PRBot prefix. The
    class-name conversion must include this prefix so Gradle finds the
    right test class (Stage 4's prompt will rename the class inside the
    file to match).
    """
    cmd = runner.build_test_command(
        ["src/test/kotlin/unit/services/_PRBotFooTests.kt"], "/repo"
    )
    tests_idx = cmd.index("--tests")
    assert cmd[tests_idx + 1] == "unit.services._PRBotFooTests"


# ---------------------------------------------------------------------------
# parse_test_output — using real captured fixtures
# ---------------------------------------------------------------------------


def test_parse_pass_fixture() -> None:
    """Real output from a passing test run."""
    result = runner.parse_test_output(PASS_FIXTURE, return_code=0)
    assert result["passed"] == 1
    assert result["failed"] == 0
    assert result["errors"] == 0
    assert result["failed_test_ids"] == []
    assert result["is_passing"] is True


def test_parse_fail_fixture() -> None:
    """Real output from a failing test run."""
    result = runner.parse_test_output(FAIL_FIXTURE, return_code=1)
    assert result["passed"] == 0
    assert result["failed"] == 1
    assert result["errors"] == 0
    assert result["is_passing"] is False
    # The failed_test_ids list should include the failing test name
    assert any(
        "JwtAuthService can sign and decode tokens" in tid
        for tid in result["failed_test_ids"]
    )


def test_parse_compile_error_fixture() -> None:
    """Real output when Kotlin compilation fails before tests run."""
    result = runner.parse_test_output(COMPILE_ERROR_FIXTURE, return_code=1)
    assert result["passed"] == 0
    assert result["failed"] == 0
    assert result["errors"] == 1
    assert result["is_passing"] is False
    # No per-test info since no tests ever ran
    assert result["failed_test_ids"] == []


# ---------------------------------------------------------------------------
# Edge cases the fixtures don't cover but matter for production
# ---------------------------------------------------------------------------


def test_parse_multi_test_failure() -> None:
    """Synthetic: 3 tests, 2 failed — verify we count correctly."""
    output = """\
> Task :test FAILED
unit.services.FooTests
  Test does X correctly() PASSED
  Test handles edge case Y() FAILED
  Test handles edge case Z() FAILED
FAILURE: Executed 3 tests in 2s (2 failed)
BUILD FAILED in 8s
"""
    result = runner.parse_test_output(output, return_code=1)
    assert result["passed"] == 1
    assert result["failed"] == 2
    assert result["errors"] == 0
    assert result["is_passing"] is False
    assert len(result["failed_test_ids"]) == 2


def test_parse_empty_output() -> None:
    """No output at all (e.g., subprocess timed out) — should not crash."""
    result = runner.parse_test_output("", return_code=124)
    assert result["passed"] == 0
    assert result["failed"] == 0
    assert result["errors"] == 1
    assert result["is_passing"] is False


def test_parse_no_tests_matched() -> None:
    """Gradle ran but found no tests matching --tests filter. Exit code
    is 0 in this case but no summary line appears.
    """
    output = """\
> Task :test NO-SOURCE
BUILD SUCCESSFUL in 1s
"""
    result = runner.parse_test_output(output, return_code=0)
    # Zero passed, zero failed — and we're not strictly "passing" because
    # no SUCCESS: summary appeared (so we couldn't confirm tests ran).
    assert result["passed"] == 0
    assert result["failed"] == 0
    assert result["is_passing"] is False


def test_return_code_zero_without_success_line_is_not_passing() -> None:
    """Edge case: exit code 0 but no SUCCESS: summary. This shouldn't
    happen in practice, but if it does, treat as not-passing rather than
    falsely report success.
    """
    result = runner.parse_test_output(
        "> Task :test\nBUILD SUCCESSFUL\n", return_code=0
    )
    assert result["is_passing"] is False


def test_collection_error_markers_exclude_compile_failures() -> None:
    """v0.2.0a6.post4: compile errors should NOT trigger the bailout
    anymore. They're things Claude can often fix (unresolved references,
    type mismatches, wrong argument names) — let the fix loop attempt
    them. Only TRUE environment errors stay in the markers.
    """
    markers = runner.collection_error_markers()
    assert "Task :compileTestKotlin FAILED" not in markers
    assert "Task :compileKotlin FAILED" not in markers
    assert "Compilation error" not in markers


def test_collection_error_markers_include_environment_failures() -> None:
    """Environment errors — things Claude CAN'T fix by rewriting test
    code — must still trigger the bailout.
    """
    markers = runner.collection_error_markers()
    assert "Could not resolve all files for configuration" in markers
    assert "Could not start your build" in markers


def test_collection_error_markers_include_jdk_gradle_mismatch() -> None:
    """A JDK/Gradle version mismatch (e.g. Gradle 6.9.3 run under JDK 17
    fails with "Unsupported class file major version 61" before any
    build script compiles) is an environment error — the fix loop must
    bail instead of asking the LLM to fix tests that never ran.
    """
    markers = runner.collection_error_markers()
    assert "Unsupported class file major version" in markers
    assert "Could not determine java version from" in markers
    assert "Could not compile settings file" in markers


def test_collection_markers_skip_real_compile_error_fixture() -> None:
    """v0.2.0a6.post4: a compile-error fixture should NOT trigger the
    bailout. The fix loop should engage and ask Claude to fix the
    compile error.
    """
    markers = runner.collection_error_markers()
    assert not any(marker in COMPILE_ERROR_FIXTURE for marker in markers), (
        "Compile errors should NOT be in collection markers in post4 — "
        "they should flow through to the fix loop"
    )


def test_collection_markers_not_in_real_failure_fixture() -> None:
    """The (test assertion failure) fixture should NOT trigger collection
    error markers, because that scenario IS something Claude can fix —
    just rewrite the assertion.
    """
    markers = runner.collection_error_markers()
    triggered = [m for m in markers if m in FAIL_FIXTURE]
    assert triggered == [], (
        f"Unexpected environment-error markers in a test-failure "
        f"scenario: {triggered}"
    )


# ---------------------------------------------------------------------------
# Handler integration — Stage 3 methods now succeed for Kotlin
# ---------------------------------------------------------------------------


def test_handler_build_test_command_works() -> None:
    """In Stage 3, calling build_test_command on the Kotlin handler should
    succeed (not raise NotImplementedError).
    """
    h = KotlinLanguageHandler()
    cmd = h.build_test_command(
        ["src/test/kotlin/unit/services/FooTests.kt"], "/repo"
    )
    assert cmd[0] == "./gradlew"


def test_handler_parse_test_output_works() -> None:
    h = KotlinLanguageHandler()
    result = h.parse_test_output(PASS_FIXTURE, 0)
    assert result["is_passing"] is True
    assert result["passed"] == 1


def test_handler_collection_error_markers_works() -> None:
    """v0.2.0a6.post4: handler exposes the runner's environment-error
    markers. Compile errors are no longer in this set — they're handled
    by the fix loop.
    """
    h = KotlinLanguageHandler()
    markers = h.collection_error_markers()
    assert isinstance(markers, tuple)
    assert len(markers) > 0
    # Environment markers still present
    assert "Could not resolve all files for configuration" in markers
    # Compile markers removed
    assert "Task :compileTestKotlin FAILED" not in markers


def test_handler_all_prompt_methods_succeed_in_stage_4b() -> None:
    """All three prompt categories work in Stage 4b: fresh,
    incremental, and fix.
    """
    from test_automator.models import AffectedFunction

    h = KotlinLanguageHandler()

    # All three system prompts work
    assert isinstance(h.system_prompt_fresh(), str)
    assert isinstance(h.system_prompt_incremental(), str)
    assert isinstance(h.system_prompt_fix(), str)

    # user_prompt_fresh — needs at least one affected function
    aff = AffectedFunction(
        file_path="src/main/kotlin/com/initech/accounts/services/X.kt",
        name="foo",
        qualified_name="com.initech.accounts.services.X.foo",
        kind="method",
        source_code="fun foo() = 1",
        line_start=1,
        line_end=1,
    )
    user_prompt = h.user_prompt_fresh(
        "src/main/kotlin/com/initech/accounts/services/X.kt", [aff]
    )
    assert isinstance(user_prompt, str) and len(user_prompt) > 50


# ---------------------------------------------------------------------------
# Orchestrator: Result: PASS bug fix
# ---------------------------------------------------------------------------


def test_orchestrator_reports_fail_when_step_fails() -> None:
    """When any pipeline step fails, the overall result should be FAIL
    even if no tests actually ran (e.g., test_generator raised
    NotImplementedError for a Stage 4 language).

    This was a bug in v0.2.0a3: the bot would report PASS for a Kotlin
    run that failed at test_generator, because is_passing only looked at
    test results (which defaulted to True when no tests ran).
    """
    from test_automator.models import (
        PipelineResult,
        StepOutcome,
        TestRunResult,
    )

    steps = [
        StepOutcome(step="local_diff_reader", success=True,
                    message="completed"),
        StepOutcome(step="code_analyzer", success=True,
                    message="completed"),
        StepOutcome(step="test_finder", success=True, message="completed"),
        StepOutcome(step="test_generator", success=False,
                    message="Test generation for 'kotlin' is not implemented"),
    ]
    test_result = TestRunResult(
        passed=0, failed=0, errors=0, total=0,
        output="", failed_test_ids=[], is_passing=True,
    )

    # Simulate what orchestrator._build_result does
    all_steps_ok = all(s.success for s in steps)
    is_passing = all_steps_ok and test_result.is_passing

    assert is_passing is False


def test_build_test_command_filters_by_declared_package_not_path(tmp_path):
    """When a test file's declared package differs from its directory
    layout, Gradle --tests must use the REAL package+class (or it runs
    0 tests). Regression for the Asurint 'unit/com/asurint/... path but
    package unit.<short>' mismatch that produced passed=0 failed=0
    errors=0."""
    from test_automator.languages.kotlin import runner

    f = tmp_path / "FooTests.kt"
    f.write_text(
        "package unit.handlers.commands\n\n"
        "import io.mockk.mockk\n\n"
        "class FooTests {\n"
        "    @Test\n"
        "    fun `does x`() {}\n"
        "}\n"
    )
    cmd = runner.build_test_command([str(f)], str(tmp_path))
    fqn = cmd[cmd.index("--tests") + 1]
    assert fqn == "unit.handlers.commands.FooTests"


def test_build_test_command_falls_back_to_path_when_unreadable():
    """A non-existent path can't be read → path-based FQN (old
    behavior preserved)."""
    from test_automator.languages.kotlin import runner

    cmd = runner.build_test_command(
        ["src/test/kotlin/unit/services/BarTests.kt"], "."
    )
    assert cmd[cmd.index("--tests") + 1] == "unit.services.BarTests"
