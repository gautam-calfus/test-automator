"""Tests for re-run idempotency: _filter_up_to_date + _stamp_manifest.

The tool must not regenerate tests for a function whose source hasn't
changed since its tests were generated. The primary signal is the in-file
coverage manifest (utils.manifest); a name-coverage + real-pass heuristic
is the fallback for test files that predate the manifest.
"""

from __future__ import annotations

from test_automator.config import LocalTestConfig
from test_automator.models import (
    AffectedFunction,
    ExistingTest,
    GeneratedTest,
    TestRunResult,
)
from test_automator.orchestrator import LocalTestPipeline
from test_automator.utils import manifest


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

BODY = "def test_foo():\n    assert foo() == 1\n"


def _manifested(fns, file_path="tests/test_mod.py", source="src/mod.py",
                comment="#", body=BODY, hashes=None):
    """Existing test whose manifest lists `fns` at their CURRENT hash
    (unless `hashes` overrides a specific one)."""
    fn_to_hash = {}
    for f in fns:
        fn_to_hash[f.qualified_name] = (
            (hashes or {}).get(f.qualified_name)
            or manifest.fn_hash(f.source_code)
        )
    content = manifest.inject(body, fn_to_hash, comment)
    return ExistingTest(
        test_file_path=file_path, source_file_path=source, content=content,
    )


# --- manifest path ---------------------------------------------------------

def test_manifest_all_current_skips_file(monkeypatch):
    pipe = _pipeline(monkeypatch, PASSING)
    fns = [_affected("foo")]
    existing = [_manifested(fns)]
    kept = pipe._filter_up_to_date(fns, existing)
    assert kept == []                 # unchanged since generated → skip
    assert pipe._runner.calls == 0    # manifest path never runs tests


def test_manifest_changed_hash_regenerates_only_that_fn(monkeypatch):
    pipe = _pipeline(monkeypatch, PASSING)
    foo, bar = _affected("foo"), _affected("bar")
    # Manifest records foo at its current hash but bar at a STALE hash.
    existing = [_manifested([foo, bar], hashes={"bar": "deadbeefdeadbeef"})]
    kept = pipe._filter_up_to_date([foo, bar], existing)
    assert [f.name for f in kept] == ["bar"]   # only the changed one
    assert pipe._runner.calls == 0


def test_manifest_missing_entry_regenerates(monkeypatch):
    pipe = _pipeline(monkeypatch, PASSING)
    foo, baz = _affected("foo"), _affected("baz")
    existing = [_manifested([foo])]            # baz absent from manifest
    kept = pipe._filter_up_to_date([foo, baz], existing)
    assert [f.name for f in kept] == ["baz"]
    assert pipe._runner.calls == 0


# --- legacy fallback (no manifest) -----------------------------------------

COVERING = "def test_foo():\n    assert foo() == 1\n"
UNRELATED = "def test_bar():\n    assert bar() == 2\n"


def test_fallback_covered_and_passing_skips(monkeypatch):
    pipe = _pipeline(monkeypatch, PASSING)
    existing = [ExistingTest(
        test_file_path="tests/test_mod.py", source_file_path="src/mod.py",
        content=COVERING,
    )]
    kept = pipe._filter_up_to_date([_affected("foo")], existing)
    assert kept == []
    assert pipe._runner.calls == 1   # had to run to confirm it passes


def test_fallback_failing_regenerates(monkeypatch):
    pipe = _pipeline(monkeypatch, FAILING)
    existing = [ExistingTest(
        test_file_path="tests/test_mod.py", source_file_path="src/mod.py",
        content=COVERING,
    )]
    kept = pipe._filter_up_to_date([_affected("foo")], existing)
    assert [f.name for f in kept] == ["foo"]


def test_fallback_uncovered_regenerates_without_running(monkeypatch):
    pipe = _pipeline(monkeypatch, PASSING)
    existing = [ExistingTest(
        test_file_path="tests/test_mod.py", source_file_path="src/mod.py",
        content=UNRELATED,
    )]
    kept = pipe._filter_up_to_date([_affected("foo")], existing)
    assert [f.name for f in kept] == ["foo"]
    assert pipe._runner.calls == 0   # cheap check bails before running


def test_no_existing_test_regenerates(monkeypatch):
    pipe = _pipeline(monkeypatch, PASSING)
    kept = pipe._filter_up_to_date([_affected("foo")], [])
    assert [f.name for f in kept] == ["foo"]
    assert pipe._runner.calls == 0


# --- stamping + round trip -------------------------------------------------

def test_stamp_then_filter_is_idempotent(monkeypatch):
    pipe = _pipeline(monkeypatch, PASSING)
    foo = _affected("foo")
    hashes = {foo.qualified_name: manifest.fn_hash(foo.source_code)}
    gen = GeneratedTest(
        source_file_path="src/mod.py",
        test_file_path="tests/test_mod.py",
        content=BODY,
        covered_functions=["foo"],
    )
    stamped = pipe._stamp_manifest(gen, hashes)
    assert "test-automator:begin" in stamped.content
    assert manifest.parse(stamped.content)["foo"] == hashes["foo"]

    # A re-run seeing the stamped file must skip.
    existing = [ExistingTest(
        test_file_path="tests/test_mod.py", source_file_path="src/mod.py",
        content=stamped.content,
    )]
    assert pipe._filter_up_to_date([foo], existing) == []


def test_stamp_uses_python_comment_token(monkeypatch):
    pipe = _pipeline(monkeypatch, PASSING)
    foo = _affected("foo")
    gen = GeneratedTest(
        source_file_path="src/mod.py",
        test_file_path="tests/test_mod.py",
        content=BODY,
        covered_functions=["foo"],
    )
    stamped = pipe._stamp_manifest(
        gen, {"foo": manifest.fn_hash(foo.source_code)}
    )
    assert stamped.content.startswith("# test-automator:begin")


def test_regenerate_passing_flag_present(monkeypatch):
    pipe = _pipeline(monkeypatch, PASSING, regenerate_passing=True)
    assert pipe._config.regenerate_passing is True
