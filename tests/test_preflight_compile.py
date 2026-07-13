"""Pre-flight: abort with a clear message when the repo's existing test
suite doesn't compile.

Real case (Asurint candidate-service): several existing Kotlin tests
failed to compile on the branch. Kotlin compiles all tests together, so
every file the tool processed reported errors=1 and the fix loop
couldn't help — it burned tokens on ~40 files that could never pass.
This guard stops that before the first LLM call.
"""

from __future__ import annotations

from test_automator.config import LocalTestConfig
from test_automator.models import AffectedFunction
from test_automator.orchestrator import LocalTestPipeline


def _fn(path: str) -> AffectedFunction:
    return AffectedFunction(
        file_path=path, name="x", qualified_name="x",
        kind="function", source_code="fun x() {}", line_start=1, line_end=1,
    )


def _pipeline(tmp_path):
    class _NoLLM:
        def generate(self, *a, **k):
            raise AssertionError("must not call the LLM before pre-flight")
    return LocalTestPipeline(
        LocalTestConfig(repo_path=str(tmp_path)), llm=_NoLLM()
    )


def test_aborts_when_kotlin_tests_do_not_compile(tmp_path, monkeypatch):
    from test_automator.languages.kotlin import runner as kt_runner

    monkeypatch.setattr(
        kt_runner, "check_tests_compile",
        lambda repo, timeout=600: (
            False,
            "e: Foo.kt: (10, 5): Unresolved reference: toGQL\n",
        ),
    )
    p = _pipeline(tmp_path)
    msg = p._preflight_compile_check(
        [_fn("src/main/kotlin/com/x/Foo.kt")]
    )
    assert msg is not None
    assert "does NOT compile" in msg
    assert "Unresolved reference: toGQL" in msg
    assert "re-run" in msg


def test_passes_when_suite_compiles(tmp_path, monkeypatch):
    from test_automator.languages.kotlin import runner as kt_runner
    monkeypatch.setattr(
        kt_runner, "check_tests_compile",
        lambda repo, timeout=600: (True, ""),
    )
    p = _pipeline(tmp_path)
    assert p._preflight_compile_check(
        [_fn("src/main/kotlin/com/x/Foo.kt")]
    ) is None


def test_skips_when_check_is_indeterminate(tmp_path, monkeypatch):
    # e.g. no gradlew wrapper → can't determine → don't block the run
    from test_automator.languages.kotlin import runner as kt_runner
    monkeypatch.setattr(
        kt_runner, "check_tests_compile",
        lambda repo, timeout=600: (None, ""),
    )
    p = _pipeline(tmp_path)
    assert p._preflight_compile_check(
        [_fn("src/main/kotlin/com/x/Foo.kt")]
    ) is None


def test_runs_check_once_per_language(tmp_path, monkeypatch):
    from test_automator.languages.kotlin import runner as kt_runner
    calls = {"n": 0}

    def fake(repo, timeout=600):
        calls["n"] += 1
        return True, ""
    monkeypatch.setattr(kt_runner, "check_tests_compile", fake)

    p = _pipeline(tmp_path)
    p._preflight_compile_check([
        _fn("src/main/kotlin/com/x/A.kt"),
        _fn("src/main/kotlin/com/x/B.kt"),
        _fn("src/main/kotlin/com/y/C.kt"),
    ])
    assert calls["n"] == 1  # one check for the whole Kotlin suite
