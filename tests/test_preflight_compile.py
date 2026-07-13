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


# --- --repair-existing ---

def test_broken_test_files_parsed_from_kotlin_output(tmp_path):
    import os
    # create the referenced test file so the parser accepts it
    tf = tmp_path / "src" / "test" / "kotlin" / "unit" / "FooTests.kt"
    tf.parent.mkdir(parents=True)
    tf.write_text("class FooTests {}")

    p = _pipeline(tmp_path)
    out = (
        f"e: {tf}: (10, 5): Unresolved reference: toGQL\n"
        f"e: {tf}: (11, 9): Type mismatch\n"
        "> Task :compileTestKotlin FAILED\n"
    )
    broken = p._broken_test_files(out)
    assert broken == ["src/test/kotlin/unit/FooTests.kt"]  # de-duped


def test_repair_existing_fixes_then_proceeds(tmp_path, monkeypatch):
    import os
    from test_automator.languages.kotlin import runner as kt_runner

    tf = tmp_path / "src" / "test" / "kotlin" / "unit" / "FooTests.kt"
    tf.parent.mkdir(parents=True)
    tf.write_text("class FooTests { broken }")

    # First compile fails; after repair, compiles.
    states = iter([
        (False, f"e: {tf}: (1, 20): error: broken\n"),  # initial
        (True, ""),                                       # after repair
    ])
    monkeypatch.setattr(
        kt_runner, "check_tests_compile",
        lambda repo, timeout=600: next(states),
    )

    cfg = LocalTestConfig(repo_path=str(tmp_path), repair_existing=True)

    class _FakeFixer:
        def _fix_one(self, gen, output):
            return gen.model_copy(update={"content": "class FooTests {}"})

    p = LocalTestPipeline(cfg, llm=type("L", (), {
        "generate": lambda *a, **k: "x"})())
    p._fixer = _FakeFixer()

    msg = p._preflight_compile_check([_fn("src/main/kotlin/com/x/Foo.kt")])
    assert msg is None  # repaired → proceed
    assert tf.read_text() == "class FooTests {}"  # file was rewritten


def test_repair_existing_still_broken_aborts_with_note(tmp_path, monkeypatch):
    from test_automator.languages.kotlin import runner as kt_runner

    tf = tmp_path / "src" / "test" / "kotlin" / "unit" / "FooTests.kt"
    tf.parent.mkdir(parents=True)
    tf.write_text("class FooTests { broken }")

    monkeypatch.setattr(
        kt_runner, "check_tests_compile",
        lambda repo, timeout=600: (False, f"e: {tf}: (1,1): error: nope\n"),
    )
    cfg = LocalTestConfig(
        repo_path=str(tmp_path), repair_existing=True, max_fix_retries=1
    )

    class _FakeFixer:
        def _fix_one(self, gen, output):
            return gen  # can't fix it

    p = LocalTestPipeline(cfg, llm=type("L", (), {
        "generate": lambda *a, **k: "x"})())
    p._fixer = _FakeFixer()

    msg = p._preflight_compile_check([_fn("src/main/kotlin/com/x/Foo.kt")])
    assert msg is not None
    assert "couldn't fix all of them" in msg
