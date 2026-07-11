"""Regression tests for two fix-loop failure modes observed live on a
CRA/Jest repo (uknowviews-react, 2026-07-08 run):

1. Prompt blow-up: the per-file fix prompt embedded the FULL runner
   output. Jest/RTL failures include complete DOM dumps, so a
   43-failure run produced 600-830k-char prompts; the LLM CLI returned
   an empty response instantly and every fix attempt silently no-oped.
   Fix: FailureFixer trims runner output to MAX_RUNNER_OUTPUT_CHARS
   (head + tail, middle elided) before building any fix prompt.

2. Regression adoption: fix attempt 1 rewrote failing files and took
   the run from 11 failed to 43 failed — and that worse test set became
   the base for attempts 2 and 3 and the final committed output.
   Fix: a round whose result has more failed+errors than the current
   state is rolled back; the next attempt retries from the best-known
   test set, and the best-known set is what gets returned.
"""

from __future__ import annotations

from types import SimpleNamespace

from test_automator.models import GeneratedTest, TestRunResult
from test_automator.steps.failure_fixer import (
    MAX_RUNNER_OUTPUT_CHARS,
    FailureFixer,
)


def _gen(content: str = "// v1") -> GeneratedTest:
    return GeneratedTest(
        source_file_path="src/utils/discount.js",
        test_file_path="src/utils/discount.test.js",
        content=content,
        covered_functions=["tierFor"],
    )


def _result(failed: int, errors: int = 0, passed: int = 1) -> TestRunResult:
    return TestRunResult(
        passed=passed,
        failed=failed,
        errors=errors,
        total=passed + failed + errors,
        output="",
        failed_test_ids=["tierFor returns 0"] * failed,
        is_passing=(failed + errors) == 0,
    )


class _QueuedRunner:
    """Returns pre-queued results, recording how many runs happened."""

    def __init__(self, results: list[TestRunResult]) -> None:
        self._results = list(results)
        self.runs = 0

    def run(self, tests: list[GeneratedTest]) -> TestRunResult:
        self.runs += 1
        return self._results.pop(0)


class _ScriptedFixer(FailureFixer):
    """FailureFixer whose LLM round is replaced by a scripted sequence
    of test lists, so fix() control flow is tested in isolation."""

    def __init__(self, config, runner, rounds: list[list[GeneratedTest]]):
        super().__init__(config, runner, llm=None)
        self._rounds = list(rounds)
        self.rounds_run = 0

    def _fix_round(self, tests, result):
        self.rounds_run += 1
        return self._rounds.pop(0)


def _config(max_fix_retries: int = 3) -> SimpleNamespace:
    return SimpleNamespace(max_fix_retries=max_fix_retries)


# ---------------------------------------------------------------------------
# Runner-output trimming
# ---------------------------------------------------------------------------


def test_short_runner_output_is_untouched() -> None:
    out = "FAIL src/x.test.js\n" * 10
    assert FailureFixer._trim_runner_output(out) == out


def test_oversized_runner_output_is_capped_keeping_head_and_tail() -> None:
    head = "H" * 400_000
    tail = "SUMMARY: Tests: 43 failed, 232 passed"
    out = head + tail

    trimmed = FailureFixer._trim_runner_output(out)

    # Bounded well below the pathological sizes (allow marker slack).
    assert len(trimmed) <= MAX_RUNNER_OUTPUT_CHARS + 200
    # Head preserved, tail (the run summary) preserved, cut is marked.
    assert trimmed.startswith("H" * 100)
    assert trimmed.endswith(tail)
    assert "trimmed" in trimmed


def test_fix_prompt_never_embeds_oversized_output() -> None:
    """End-to-end through _fix_one: the LLM must receive a bounded
    prompt even when the runner output is hundreds of kchars."""
    captured: dict[str, str] = {}

    class _CapturingLLM:
        def generate(self, system_prompt: str, user_prompt: str) -> str:
            captured["user"] = user_prompt
            return (
                "```js\n"
                "import { tierFor } from './discount';\n"
                "test('tierFor returns 0', () => {\n"
                "  expect(tierFor(0)).toBe(0);\n"
                "});\n"
                "```"
            )

    fixer = FailureFixer(_config(), runner=None, llm=_CapturingLLM())
    huge = "X" * 800_000 + "\nTests: 43 failed"
    fixer._fix_one(_gen(), huge)

    # The prompt also embeds source + test content; the runner-output
    # share of it must be capped, so the whole prompt stays far below
    # the observed 600-830k pathological sizes.
    assert len(captured["user"]) < 100_000


# ---------------------------------------------------------------------------
# Regression rollback
# ---------------------------------------------------------------------------


def test_worse_fix_round_is_rolled_back() -> None:
    """11 failed -> round produces 43 failed -> the worse tests must
    not be kept; the original set is returned."""
    original = [_gen("// original")]
    worse = [_gen("// regressed rewrite")]

    runner = _QueuedRunner([_result(failed=43)])
    fixer = _ScriptedFixer(_config(max_fix_retries=1), runner, [worse])

    final_tests, final_result = fixer.fix(original, _result(failed=11))

    assert final_tests[0].content == "// original"
    assert final_result.failed == 11


def test_rolled_back_round_retries_from_best_known_tests() -> None:
    """Attempt 1 regresses (rolled back); attempt 2 must start from the
    original tests, not the regressed ones, and its improvement is kept."""
    original = [_gen("// original")]
    worse = [_gen("// regressed")]
    fixed = [_gen("// fixed")]

    seen_bases: list[str] = []

    class _RecordingFixer(_ScriptedFixer):
        def _fix_round(self, tests, result):
            seen_bases.append(tests[0].content)
            return super()._fix_round(tests, result)

    runner = _QueuedRunner([_result(failed=43), _result(failed=0, passed=12)])
    fixer = _RecordingFixer(
        _config(max_fix_retries=3), runner, [worse, fixed]
    )

    final_tests, final_result = fixer.fix(original, _result(failed=11))

    assert seen_bases == ["// original", "// original"]
    assert final_tests[0].content == "// fixed"
    assert final_result.is_passing is True


def test_improving_fix_round_is_adopted() -> None:
    original = [_gen("// original")]
    better = [_gen("// improved")]

    runner = _QueuedRunner([_result(failed=2)])
    fixer = _ScriptedFixer(_config(max_fix_retries=1), runner, [better])

    final_tests, final_result = fixer.fix(original, _result(failed=11))

    assert final_tests[0].content == "// improved"
    assert final_result.failed == 2


def test_equal_score_round_is_adopted_not_rolled_back() -> None:
    """Same failed+errors count is not a regression — the rewrite may
    fix one test and break another; keep it as the new base rather
    than looping on an identical retry."""
    original = [_gen("// original")]
    sideways = [_gen("// sideways rewrite")]

    runner = _QueuedRunner([_result(failed=11)])
    fixer = _ScriptedFixer(_config(max_fix_retries=1), runner, [sideways])

    final_tests, _ = fixer.fix(original, _result(failed=11))

    assert final_tests[0].content == "// sideways rewrite"


def test_clearing_a_collection_error_is_progress_not_regression() -> None:
    """The uknowviews-react bug: initial run is errors=1 (whole file
    won't compile, 0 tests ran); the fix makes it compile so tests now
    RUN but many fail (failed=28). That is forward progress — the
    candidate must be ADOPTED, not rolled back to the uncompilable
    file. Weighting errors above failures is what makes this hold."""
    original = [_gen("// uncompilable")]
    compiles = [_gen("// compiles, 28 assertions fail")]

    # initial: errors=1 (0 ran). candidate: failed=28, errors=0.
    runner = _QueuedRunner([_result(failed=28, errors=0, passed=0)])
    fixer = _ScriptedFixer(_config(max_fix_retries=1), runner, [compiles])

    final_tests, final_result = fixer.fix(
        original, _result(failed=0, errors=1, passed=0)
    )

    assert final_tests[0].content == "// compiles, 28 assertions fail"
    assert final_result.errors == 0
    assert final_result.failed == 28


def test_introducing_a_collection_error_is_rolled_back() -> None:
    """The inverse: a run that was executing (failed=3) must not be
    replaced by one that no longer compiles (errors=1), even though 1 <
    3 by raw count — errors weigh more."""
    original = [_gen("// runs, 3 fail")]
    broke = [_gen("// now uncompilable")]

    runner = _QueuedRunner([_result(failed=0, errors=1, passed=0)])
    fixer = _ScriptedFixer(_config(max_fix_retries=1), runner, [broke])

    final_tests, final_result = fixer.fix(
        original, _result(failed=3, errors=0)
    )

    assert final_tests[0].content == "// runs, 3 fail"
    assert final_result.failed == 3
    assert final_result.errors == 0
