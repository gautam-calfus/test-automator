"""Last-resort pruning: ship the tests that pass, drop the ones that
don't.

Before this, a run whose fix loop couldn't fully converge produced
NOTHING usable — the orchestrator left the whole file "for review" and
the entire run's LLM spend was wasted, even when 34 of 37 tests passed.
A human in that position keeps the 34 and deletes the 3. That's what
this does, and only when it can verify the result is green.
"""

from __future__ import annotations

import pytest

from test_automator.config import LocalTestConfig
from test_automator.models import GeneratedTest, TestRunResult
from test_automator.steps import failure_fixer as ff_module
from test_automator.steps.failure_fixer import FailureFixer


class _Test:
    """Stand-in for a parsed test method."""

    def __init__(self, name):
        self.name = name


class FakeHandler:
    def __init__(self):
        self.removed = None

    def parse_existing_tests(self, content):
        # one test per line of the form "TEST <name>"
        return [
            _Test(line.split()[1])
            for line in content.splitlines()
            if line.startswith("TEST ")
        ]

    def remove_tests(self, content, to_remove):
        self.removed = [t.name for t in to_remove]
        drop = {t.name for t in to_remove}
        return "\n".join(
            line for line in content.splitlines()
            if not (line.startswith("TEST ") and line.split()[1] in drop)
        )


class FakeRunner:
    """Returns queued results for successive run() calls."""

    def __init__(self, results):
        self._results = list(results)
        self.runs = []

    def run(self, tests):
        self.runs.append(tests[0].content if tests else "")
        return self._results.pop(0)


def _result(passed=0, failed=0, errors=0, ids=()):
    return TestRunResult(
        passed=passed,
        failed=failed,
        errors=errors,
        total=passed + failed,
        output="",
        failed_test_ids=list(ids),
        is_passing=(failed == 0 and errors == 0 and passed > 0),
    )


def _gen(content):
    return GeneratedTest(
        source_file_path="src/main/java/com/acme/AdminService.java",
        test_file_path="src/test/java/com/acme/AdminServiceTest.java",
        content=content,
        covered_functions=["AdminService.configure"],
    )


@pytest.fixture
def fixer_env(monkeypatch):
    handler = FakeHandler()
    monkeypatch.setattr(
        ff_module, "get_handler_for_file", lambda path: handler
    )

    def make(runner):
        cfg = LocalTestConfig(repo_path="/tmp/x", max_fix_retries=0)
        return FailureFixer(cfg, runner, llm=None), handler

    return make


CONTENT = "HEADER\nTEST alpha\nTEST beta\nTEST gamma\n"


def test_failing_tests_are_dropped_and_rest_kept(fixer_env):
    runner = FakeRunner([_result(passed=2)])  # green after pruning
    fixer, handler = fixer_env(runner)
    failing = _result(
        passed=2, failed=1,
        ids=["com.acme.AdminServiceTest.beta()"],
    )

    tests, result = fixer._prune_failing_tests([_gen(CONTENT)], failing)

    assert handler.removed == ["beta"]
    assert "TEST alpha" in tests[0].content
    assert "TEST gamma" in tests[0].content
    assert "TEST beta" not in tests[0].content
    assert result.is_passing


def test_pytest_style_ids_are_matched(fixer_env):
    runner = FakeRunner([_result(passed=2)])
    fixer, handler = fixer_env(runner)
    failing = _result(
        passed=2, failed=1, ids=["tests/test_x.py::beta"]
    )

    fixer._prune_failing_tests([_gen(CONTENT)], failing)

    assert handler.removed == ["beta"]


def test_compile_errors_are_never_pruned(fixer_env):
    """errors>0 means nothing ran — no individual test can be blamed."""
    runner = FakeRunner([])
    fixer, _ = fixer_env(runner)
    broken = _result(errors=1, ids=[])

    tests, result = fixer._prune_failing_tests([_gen(CONTENT)], broken)

    assert tests[0].content == CONTENT
    assert result is broken
    assert runner.runs == []  # no re-run attempted


def test_all_tests_failing_is_not_pruned(fixer_env):
    """Don't ship an empty test class."""
    runner = FakeRunner([])
    fixer, _ = fixer_env(runner)
    failing = _result(
        failed=3,
        ids=[
            "com.acme.AdminServiceTest.alpha()",
            "com.acme.AdminServiceTest.beta()",
            "com.acme.AdminServiceTest.gamma()",
        ],
    )

    tests, _ = fixer._prune_failing_tests([_gen(CONTENT)], failing)

    assert tests[0].content == CONTENT
    assert runner.runs == []


def test_pruning_that_stays_red_reverts_to_original(fixer_env):
    """If removing the named failures doesn't produce green (e.g. a
    cascading failure), keep the original file for human review rather
    than shipping a half-mangled one."""
    runner = FakeRunner([_result(passed=1, failed=1, ids=["x.gamma()"])])
    fixer, _ = fixer_env(runner)
    failing = _result(
        passed=2, failed=1, ids=["com.acme.AdminServiceTest.beta()"]
    )

    tests, result = fixer._prune_failing_tests([_gen(CONTENT)], failing)

    assert tests[0].content == CONTENT
    assert result is failing


def test_unmatched_failure_names_change_nothing(fixer_env):
    runner = FakeRunner([])
    fixer, _ = fixer_env(runner)
    failing = _result(
        passed=3, failed=1, ids=["com.other.SomethingElseTest.zzz()"]
    )

    tests, result = fixer._prune_failing_tests([_gen(CONTENT)], failing)

    assert tests[0].content == CONTENT
    assert result is failing
    assert runner.runs == []
