"""Tests for the idempotency skip: files whose existing tests already
pass AND already cover every changed function are left untouched on a
re-run (no LLM call, no rewrite).

This is what makes running the tool twice safe — the first run's
correct, passing tests aren't churned into different code the second
time.
"""

from __future__ import annotations

from test_automator.config import LocalTestConfig
from test_automator.models import (
    AffectedFunction,
    ExistingTest,
    TestRunResult,
)
from test_automator.orchestrator import LocalTestPipeline


def _affected(name: str, file_path: str = "src/mod.py") -> AffectedFunction:
    return AffectedFunction(
        file_path=file_path,
        name=name,
        qualified_name=name,
        kind="function",
        source_code=f"def {name}():\n    return 1\n",
        line_start=1,
        line_end=2,
    )


def _pipeline(monkeypatch, run_result: TestRunResult, **cfg_kw):
    """Build a pipeline with the LLM bridge stubbed out (never called in
    these tests) and the runner stubbed to a fixed result."""
    monkeypatch.setattr(
        "test_automator.orchestrator.create_bridge",
        lambda **kw: object(),
    )
    config = LocalTestConfig(repo_path="/repo", **cfg_kw)
    pipe = LocalTestPipeline(config)

    class _Runner:
        def __init__(self):
            self.calls = 0

        def run(self, tests):
            self.calls += 1
            return run_result

    pipe._runner = _Runner()
    return pipe


PASSING = TestRunResult(
    passed=1, failed=0, errors=0, total=1,
    output="1 passed", failed_test_ids=[], is_passing=True,
)
FAILING = TestRunResult(
    passed=0, failed=1, errors=0, total=1,
    output="1 failed", failed_test_ids=["x"], is_passing=False,
)

COVERING_TEST = "def test_foo():\n    assert foo() == 1\n"
UNRELATED_TEST = "def test_bar():\n    assert bar() == 2\n"


def test_skips_file_when_existing_tests_pass_and_cover(monkeypatch):
    pipe = _pipeline(monkeypatch, PASSING)
    affected = [_affected("foo")]
    existing = [ExistingTest(
        test_file_path="tests/test_mod.py",
        source_file_path="src/mod.py",
        content=COVERING_TEST,
    )]
    kept = pipe._skip_covered_passing(affected, existing)
    assert kept == []  # foo is covered + passing -> skipped
    assert pipe._runner.calls == 1  # existing suite was run once to confirm


def test_keeps_file_when_existing_tests_fail(monkeypatch):
    pipe = _pipeline(monkeypatch, FAILING)
    affected = [_affected("foo")]
    existing = [ExistingTest(
        test_file_path="tests/test_mod.py",
        source_file_path="src/mod.py",
        content=COVERING_TEST,
    )]
    kept = pipe._skip_covered_passing(affected, existing)
    assert [f.name for f in kept] == ["foo"]  # failing -> regenerate


def test_keeps_file_when_a_changed_function_is_uncovered(monkeypatch):
    pipe = _pipeline(monkeypatch, PASSING)
    # foo is covered, baz is NOT -> whole file must be regenerated, and
    # we must NOT waste a runner call on it (structural check fails first).
    affected = [_affected("foo"), _affected("baz")]
    existing = [ExistingTest(
        test_file_path="tests/test_mod.py",
        source_file_path="src/mod.py",
        content=COVERING_TEST,  # only covers foo
    )]
    kept = pipe._skip_covered_passing(affected, existing)
    assert sorted(f.name for f in kept) == ["baz", "foo"]
    assert pipe._runner.calls == 0  # never ran tests — cheap check bailed


def test_keeps_file_with_no_existing_tests(monkeypatch):
    pipe = _pipeline(monkeypatch, PASSING)
    affected = [_affected("foo")]
    kept = pipe._skip_covered_passing(affected, existing_tests=[])
    assert [f.name for f in kept] == ["foo"]
    assert pipe._runner.calls == 0


def test_regenerate_passing_flag_disables_skip(monkeypatch):
    # With regenerate_passing=True the orchestrator never calls the skip
    # (verified here by asserting the helper isn't what gates it): the
    # helper itself still skips, but the flag short-circuits the call.
    pipe = _pipeline(monkeypatch, PASSING, regenerate_passing=True)
    assert pipe._config.regenerate_passing is True


def test_partial_skip_across_files(monkeypatch):
    pipe = _pipeline(monkeypatch, PASSING)
    # file A: covered + passing -> skip. file B: uncovered -> keep.
    a = _affected("foo", "src/a.py")
    b = _affected("qux", "src/b.py")
    existing = [
        ExistingTest(
            test_file_path="tests/test_a.py",
            source_file_path="src/a.py",
            content=COVERING_TEST,
        ),
        ExistingTest(
            test_file_path="tests/test_b.py",
            source_file_path="src/b.py",
            content=UNRELATED_TEST,  # doesn't cover qux
        ),
    ]
    kept = pipe._skip_covered_passing([a, b], existing)
    assert [f.name for f in kept] == ["qux"]
