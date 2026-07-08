"""Tests for the per-file generate → run → fix pipeline.

Old flow: generate ALL files, run everything once at the end, then fix.
One broken file's compile error made every file's run fail, failure
attribution had to guess which file was at fault, and the fixer burned
LLM calls re-fixing files that already passed.

New flow: each generated file's tests run (and get fixed) immediately,
before the next file is generated; a final combined run remains as the
commit gate (skipped when only one file was produced).
"""

from __future__ import annotations

from test_automator.config import LocalTestConfig
from test_automator.models import GeneratedTest, TestRunResult
from test_automator.orchestrator import LocalTestPipeline


def _gen(name: str) -> GeneratedTest:
    return GeneratedTest(
        source_file_path=f"src/{name}.py",
        test_file_path=f"tests/test_{name}.py",
        content=f"# tests for {name}\n",
        covered_functions=[name],
    )


def _result(passed: int, failed: int) -> TestRunResult:
    return TestRunResult(
        passed=passed,
        failed=failed,
        errors=0,
        total=passed + failed,
        output="",
        failed_test_ids=[],
        is_passing=failed == 0,
    )


class _FakeGenerator:
    def __init__(self, gens):
        self._gens = gens

    def iter_generate(self, affected, existing, removed=None):
        yield from self._gens


class _FakeRunner:
    """Records every run() call; fails any file whose name says so."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def run(self, tests):
        self.calls.append([t.test_file_path for t in tests])
        failing = any("failing" in t.test_file_path for t in tests)
        return _result(passed=1, failed=1 if failing else 0)


class _FakeFixer:
    def __init__(self):
        self.fixed_files: list[str] = []

    def fix(self, tests, result):
        self.fixed_files.extend(t.test_file_path for t in tests)
        fixed = [
            t.model_copy(
                update={
                    "test_file_path": t.test_file_path.replace(
                        "failing", "fixed"
                    )
                }
            )
            for t in tests
        ]
        return fixed, _result(passed=2, failed=0)


def _pipeline(tmp_path, gens, runner, fixer):
    class _NoLLM:
        def generate(self, *_a, **_k):
            raise AssertionError("LLM must not be called")

    p = LocalTestPipeline(
        LocalTestConfig(repo_path=str(tmp_path)), llm=_NoLLM()
    )
    p._generator = _FakeGenerator(gens)
    p._runner = runner
    p._fixer = fixer
    return p


def test_each_file_runs_before_next_is_processed(tmp_path) -> None:
    runner = _FakeRunner()
    fixer = _FakeFixer()
    p = _pipeline(tmp_path, [_gen("alpha"), _gen("beta")], runner, fixer)

    tests, solo = p._generate_run_fix([], [], [])

    # One run per file, each with exactly that file
    assert runner.calls == [
        ["tests/test_alpha.py"],
        ["tests/test_beta.py"],
    ]
    assert [t.test_file_path for t in tests] == [
        "tests/test_alpha.py",
        "tests/test_beta.py",
    ]
    # Two files → no reusable solo result (combined run must happen)
    assert solo is None
    # Nothing failed → fixer never engaged
    assert fixer.fixed_files == []


def test_fixer_engages_only_for_the_failing_file(tmp_path) -> None:
    runner = _FakeRunner()
    fixer = _FakeFixer()
    p = _pipeline(
        tmp_path,
        [_gen("ok_one"), _gen("failing_two"), _gen("ok_three")],
        runner,
        fixer,
    )

    tests, _ = p._generate_run_fix([], [], [])

    assert fixer.fixed_files == ["tests/test_failing_two.py"]
    # The fixed version replaces the failing one in the final list
    assert [t.test_file_path for t in tests] == [
        "tests/test_ok_one.py",
        "tests/test_fixed_two.py",
        "tests/test_ok_three.py",
    ]


def test_single_file_result_is_reused_as_final(tmp_path) -> None:
    runner = _FakeRunner()
    fixer = _FakeFixer()
    p = _pipeline(tmp_path, [_gen("solo")], runner, fixer)

    tests, solo = p._generate_run_fix([], [], [])

    assert len(tests) == 1
    assert solo is not None and solo.is_passing
    # Only the per-file run happened so far
    assert runner.calls == [["tests/test_solo.py"]]
