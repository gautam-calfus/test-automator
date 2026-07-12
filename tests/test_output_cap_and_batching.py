"""Tests for the two v0.3.0 fixes born from the Acme services run:

1. **Output-token cap**: Claude Code caps responses at 32K output
   tokens by default. Large generated test files exceed that. The
   bridge now sets CLAUDE_CODE_MAX_OUTPUT_TOKENS (default 16000) on
   the subprocess env, unless the user already set it.

2. **Batched generation**: QuestionRoutingService had 7 changed
   methods; one giant prompt asked for tests covering all of them and
   the response blew the cap anyway. The generator now splits diffs
   with more than MAX_FUNCTIONS_PER_CALL functions into several calls:
   fresh generation for the first batch, incremental additions for the
   rest.
"""

from __future__ import annotations

import subprocess

import pytest

from test_automator.config import LocalTestConfig
from test_automator.llm_bridge import ClaudeCodeBridge
from test_automator.models import AffectedFunction, ExistingTest
from test_automator.steps import test_generator as tg_module
from test_automator.steps.test_generator import (
    MAX_FUNCTIONS_PER_CALL,
    TestGenerator,
)


# ---------------------------------------------------------------------------
# Output-token cap
# ---------------------------------------------------------------------------


def _fake_run_capture(captured):
    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
    return fake_run


def test_bridge_sets_max_output_tokens_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS", raising=False)
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _fake_run_capture(captured))

    bridge = ClaudeCodeBridge(cmd="echo", timeout=5)
    bridge.generate("system", "user")

    assert captured["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "16000"


def test_bridge_respects_user_env_value(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "12345")
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _fake_run_capture(captured))

    bridge = ClaudeCodeBridge(cmd="echo", timeout=5)
    bridge.generate("system", "user")

    assert captured["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "12345"


def test_bridge_custom_cap(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS", raising=False)
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _fake_run_capture(captured))

    bridge = ClaudeCodeBridge(cmd="echo", timeout=5, max_output_tokens=99_000)
    bridge.generate("system", "user")

    assert captured["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "99000"


def test_config_wires_max_output_tokens():
    config = LocalTestConfig(repo_path="/tmp/x")
    assert config.claude_code_max_output_tokens == 16_000


# ---------------------------------------------------------------------------
# Batched generation
# ---------------------------------------------------------------------------


def _fn(i: int) -> AffectedFunction:
    return AffectedFunction(
        file_path="src/main/java/com/acme/FooService.java",
        name=f"method{i}",
        qualified_name=f"FooService.method{i}",
        kind="method",
        source_code=f"public void method{i}() {{}}",
        line_start=i * 10,
        line_end=i * 10 + 5,
    )


class FakeHandler:
    """Minimal handler implementing everything the generator touches."""

    name = "fake"

    def user_prompt_fresh(self, source_path, functions):
        return "FRESH: " + ",".join(f.name for f in functions)

    def system_prompt_fresh(self):
        return "sys-fresh"

    def user_prompt_incremental(
        self, source_path, existing, functions, trimmed="", removed=""
    ):
        return "INCR: " + ",".join(f.name for f in functions)

    def system_prompt_incremental(self):
        return "sys-incr"

    def extract_code(self, raw, mode):
        return raw

    def parse_existing_tests(self, content):
        return []

    def covers(self, test_name, source_function_name):
        return False

    def extract_test_source(self, content, tests):
        return ""

    def remove_tests(self, content, to_remove):
        return content

    def merge_new_tests(self, existing, new_tests):
        return existing + "\n" + new_tests


class RecordingLLM:
    """Echoes prompts back and records every call's user prompt."""

    def __init__(self, fail_on_call: int | None = None):
        self.calls: list[str] = []
        self._fail_on_call = fail_on_call

    def generate(self, system_prompt, user_prompt):
        self.calls.append(user_prompt)
        if self._fail_on_call == len(self.calls):
            raise RuntimeError("simulated LLM failure")
        return f"<<{user_prompt}>>"


class FakeFinder:
    def suggest_test_path(self, source_path, existing=None):
        return "src/test/java/com/acme/FooServiceTest.java"


@pytest.fixture
def generator_env(monkeypatch):
    handler = FakeHandler()
    monkeypatch.setattr(
        tg_module, "get_handler_for_file", lambda path: handler
    )
    config = LocalTestConfig(repo_path="/tmp/x")

    def make(llm):
        return TestGenerator(config, FakeFinder(), llm)

    return make


def test_small_diff_uses_single_call(generator_env):
    llm = RecordingLLM()
    gen = generator_env(llm)

    results = gen.generate([_fn(i) for i in range(3)], existing_tests=[])

    assert len(llm.calls) == 1
    assert llm.calls[0].startswith("FRESH:")
    assert len(results) == 1
    assert len(results[0].covered_functions) == 3


def test_large_diff_is_batched(generator_env):
    """7 changed functions with a cap of 4 → 2 calls: one fresh
    (4 functions), one incremental (3 functions). All 7 covered."""
    llm = RecordingLLM()
    gen = generator_env(llm)
    functions = [_fn(i) for i in range(7)]

    results = gen.generate(functions, existing_tests=[])

    assert len(llm.calls) == 2
    assert llm.calls[0] == "FRESH: method0,method1,method2,method3"
    assert llm.calls[1] == "INCR: method4,method5,method6"
    assert len(results) == 1
    assert len(results[0].covered_functions) == 7
    # Later batches are merged into the first batch's file
    assert "FRESH:" in results[0].content
    assert "INCR:" in results[0].content


def test_batch_size_matches_constant(generator_env):
    llm = RecordingLLM()
    gen = generator_env(llm)
    functions = [_fn(i) for i in range(MAX_FUNCTIONS_PER_CALL * 2 + 1)]

    gen.generate(functions, existing_tests=[])

    assert len(llm.calls) == 3  # 4 + 4 + 1


def test_later_batch_failure_keeps_earlier_work(generator_env):
    """A failure in batch 2 must NOT lose batch 1's tests — partial
    coverage beats losing 20 minutes of generation."""
    llm = RecordingLLM(fail_on_call=2)
    gen = generator_env(llm)
    functions = [_fn(i) for i in range(7)]

    results = gen.generate(functions, existing_tests=[])

    assert len(results) == 1
    assert len(results[0].covered_functions) == 4  # batch 1 only
    assert "FRESH:" in results[0].content


def test_first_batch_failure_is_fatal_for_file(generator_env):
    """Batch 1 failing means nothing was generated for the file — the
    per-file error path (skip + warn) applies as before."""
    llm = RecordingLLM(fail_on_call=1)
    gen = generator_env(llm)

    with pytest.raises(Exception):
        gen.generate([_fn(i) for i in range(7)], existing_tests=[])


def test_existing_test_file_batches_incrementally(generator_env):
    """With a real existing test file, every batch is incremental."""
    llm = RecordingLLM()
    gen = generator_env(llm)
    functions = [_fn(i) for i in range(5)]
    existing = ExistingTest(
        test_file_path="src/test/java/com/acme/FooServiceTest.java",
        source_file_path="src/main/java/com/acme/FooService.java",
        content="class FooServiceTest {}",
    )

    results = gen.generate(functions, existing_tests=[existing])

    assert len(llm.calls) == 2
    assert llm.calls[0].startswith("INCR:")
    assert llm.calls[1].startswith("INCR:")
    assert len(results[0].covered_functions) == 5
