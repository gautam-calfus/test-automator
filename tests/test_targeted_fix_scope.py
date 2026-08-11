"""Fix-loop repairs must be TARGETED at the failing tests.

Observed failure that motivated this: a run with 5 failing tests out of
37 spent all three retries crawling 5 → 4 → 3. Each round asked for a
full-file rewrite without naming the culprits, so the model re-derived
them from raw runner output and rewrote everything — repairing one test
while disturbing others.

Now the failing method names are passed to the fix prompt, which states
that every other test currently passes and must be reproduced
unchanged.
"""

from __future__ import annotations

from test_automator.languages.java import prompts
from test_automator.models import GeneratedTest, TestRunResult
from test_automator.steps.failure_fixer import (
    FailureFixer,
    _accepts_failing_names,
)


TEST_FILE = """package com.acme.service;

class AdminServiceTest {
    @Test
    void shouldCallConfigureAlertScheduleWhenUserIsCm() {}

    @Test
    void shouldNotCallConfigureAlertScheduleWhenUserIsNotCm() {}

    @Test
    void somethingThatPasses() {}
}
"""


def _gen() -> GeneratedTest:
    return GeneratedTest(
        source_file_path="src/main/java/com/acme/service/AdminService.java",
        test_file_path="src/test/java/com/acme/service/AdminServiceTest.java",
        content=TEST_FILE,
        covered_functions=["AdminService.configureUserGeoRoutingAtrs"],
    )


def _result(ids: list[str]) -> TestRunResult:
    return TestRunResult(
        passed=1,
        failed=len(ids),
        errors=0,
        total=1 + len(ids),
        output="boom",
        failed_test_ids=ids,
        is_passing=False,
    )


def test_failing_names_extracted_from_junit_ids():
    result = _result([
        "com.acme.service.AdminServiceTest."
        "shouldCallConfigureAlertScheduleWhenUserIsCm()",
        "com.acme.service.AdminServiceTest."
        "shouldNotCallConfigureAlertScheduleWhenUserIsNotCm()",
    ])

    names = FailureFixer._failing_names_for(_gen(), result)

    assert names == [
        "shouldCallConfigureAlertScheduleWhenUserIsCm",
        "shouldNotCallConfigureAlertScheduleWhenUserIsNotCm",
    ]


def test_failing_names_ignores_other_files_tests():
    """Ids from a different test class must not be attributed here."""
    result = _result(["com.acme.other.CMServiceTest.someOtherTest()"])

    assert FailureFixer._failing_names_for(_gen(), result) == []


def test_failing_names_handles_pytest_style_ids():
    result = _result(["tests/test_x.py::somethingThatPasses"])

    assert FailureFixer._failing_names_for(_gen(), result) == [
        "somethingThatPasses"
    ]


def test_failing_names_deduplicates():
    result = _result([
        "com.acme.service.AdminServiceTest.somethingThatPasses()",
        "com.acme.service.AdminServiceTest.somethingThatPasses()",
    ])

    assert FailureFixer._failing_names_for(_gen(), result) == [
        "somethingThatPasses"
    ]


def test_prompt_names_failing_tests_and_protects_the_rest(tmp_path):
    src = tmp_path / "AdminService.java"
    src.write_text("package com.acme.service;\nclass AdminService {}\n")
    gen = GeneratedTest(
        source_file_path=str(src),
        test_file_path="src/test/java/com/acme/service/AdminServiceTest.java",
        content=TEST_FILE,
        covered_functions=[],
    )

    prompt = prompts.user_prompt_fix(
        gen,
        "Wanted but not invoked",
        ["shouldCallConfigureAlertScheduleWhenUserIsCm"],
    )

    assert "THE ONLY TESTS YOU MAY CHANGE" in prompt
    assert "shouldCallConfigureAlertScheduleWhenUserIsCm" in prompt
    assert "byte-for-byte" in prompt
    # And it still tells the model to check the real source first
    assert "READ the real source" in prompt


def test_prompt_without_names_still_constrains_scope(tmp_path):
    """Backwards compatibility: no names available (e.g. a compile
    error with no attributable test) still discourages a free rewrite."""
    src = tmp_path / "AdminService.java"
    src.write_text("package com.acme.service;\nclass AdminService {}\n")
    gen = GeneratedTest(
        source_file_path=str(src),
        test_file_path="src/test/java/com/acme/service/AdminServiceTest.java",
        content=TEST_FILE,
        covered_functions=[],
    )

    prompt = prompts.user_prompt_fix(gen, "COMPILATION ERROR")

    assert "SCOPE" in prompt
    assert "currently-passing test" in prompt


def test_system_fix_prompt_forbids_touching_passing_tests():
    assert "PRESERVE THE PASSING TESTS" in prompts.SYSTEM_PROMPT_FIX


def test_java_handler_accepts_failing_names():
    """The dispatcher only forwards names to handlers that take them."""
    from test_automator.languages.java.handler import JavaLanguageHandler

    assert _accepts_failing_names(JavaLanguageHandler().user_prompt_fix)


def test_handlers_without_the_arg_are_not_broken():
    """A two-arg handler must not be called with three arguments."""

    def legacy_user_prompt_fix(generated, runner_output):
        return "legacy"

    assert not _accepts_failing_names(legacy_user_prompt_fix)
