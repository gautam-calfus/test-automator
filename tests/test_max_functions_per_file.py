"""Tests for --max-functions-per-file (fewer, reviewable tests).

The uknowviews-react run's actions.js had 28 changed functions, which
fanned out into 7 escalating LLM calls and 205 generated tests. Capping
functions-per-file keeps a single large module from dominating a run's
token budget and producing an unreviewable pile of tests; skipped
functions are logged so coverage is bounded honestly, not silently.
"""

from __future__ import annotations

from types import SimpleNamespace

from test_automator.models import AffectedFunction
from test_automator.steps.test_generator import TestGenerator


def _fn(name: str) -> AffectedFunction:
    return AffectedFunction(
        file_path="src/Action/actions.js",
        name=name,
        qualified_name=name,
        kind="function",
        source_code="export const %s = () => ({});" % name,
        line_start=1,
        line_end=1,
    )


def _generator(cap: int) -> TestGenerator:
    cfg = SimpleNamespace(
        max_functions_per_file=cap,
        all_test_dirs=["tests"],
        repo_path="/tmp/x",
    )
    return TestGenerator(cfg, test_finder=None, llm=None)


def test_caps_functions_and_keeps_source_order() -> None:
    gen = _generator(cap=10)
    fns = [_fn(f"action{i}") for i in range(28)]

    kept = gen._cap_functions("src/Action/actions.js", fns)

    assert len(kept) == 10
    assert [f.name for f in kept] == [f"action{i}" for i in range(10)]


def test_no_cap_when_under_limit() -> None:
    gen = _generator(cap=10)
    fns = [_fn(f"a{i}") for i in range(5)]
    assert gen._cap_functions("f.js", fns) == fns


def test_zero_means_unlimited() -> None:
    gen = _generator(cap=0)
    fns = [_fn(f"a{i}") for i in range(50)]
    assert len(gen._cap_functions("f.js", fns)) == 50


def test_cap_logs_skipped_functions(caplog) -> None:
    import logging

    gen = _generator(cap=3)
    fns = [_fn(n) for n in ("a", "b", "c", "d", "e")]

    with caplog.at_level(logging.WARNING, logger="test_automator"):
        gen._cap_functions("src/Action/actions.js", fns)

    msg = caplog.text
    assert "SKIPPED 2" in msg
    # names of the dropped functions appear so the user can target them
    assert "d" in msg and "e" in msg
    assert "--file" in msg
