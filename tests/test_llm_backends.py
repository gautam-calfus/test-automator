"""Tests for the multi-backend LLM bridge (v0.3.0).

The pipeline is model-agnostic: language handlers build prompts and
parse responses; the bridge just shells out to a CLI. These tests pin
the exact argv each provider bridge produces, the system-prompt
flattening for CLIs without a system-prompt flag, and the factory's
provider dispatch.

All tests monkeypatch subprocess.run — no real CLI is invoked. The
bridges are constructed with cmd="echo" so the PATH check passes on
any machine.
"""

from __future__ import annotations

import subprocess

import pytest

from test_automator.llm_bridge import (
    KNOWN_PROVIDERS,
    ClaudeCodeBridge,
    CopilotCliBridge,
    GeminiCliBridge,
    GenericCliBridge,
    create_bridge,
)
from test_automator.utils.exceptions import LLMBridgeError


@pytest.fixture
def captured(monkeypatch):
    """Capture the argv and kwargs of the subprocess call."""
    record: dict = {}

    def fake_run(cmd, **kwargs):
        record["argv"] = cmd
        record.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="response", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return record


def test_copilot_argv_uses_programmatic_mode(captured):
    bridge = CopilotCliBridge(cmd="echo", timeout=5)

    out = bridge.generate("SYSTEM RULES", "USER TASK")

    assert out == "response"
    assert captured["argv"][0] == "echo"
    assert captured["argv"][1] == "-p"
    prompt = captured["argv"][2]
    # System prompt folded in (no --system-prompt flag on copilot)
    assert "SYSTEM RULES" in prompt
    assert "USER TASK" in prompt
    # No tool-permission flags: tool calls must be denied, text-only
    assert "--allow-all-tools" not in captured["argv"]


def test_gemini_argv_uses_headless_mode(captured):
    bridge = GeminiCliBridge(cmd="echo", timeout=5)

    bridge.generate("SYSTEM RULES", "USER TASK")

    assert captured["argv"][1] == "-p"
    assert "SYSTEM RULES" in captured["argv"][2]
    assert "USER TASK" in captured["argv"][2]
    assert "--yolo" not in captured["argv"]


def test_claude_argv_keeps_agent_disabling_flags(captured):
    bridge = ClaudeCodeBridge(cmd="echo", timeout=5)

    bridge.generate("SYSTEM RULES", "USER TASK")

    argv = captured["argv"]
    assert "--print" in argv
    assert "--tools" in argv
    assert argv[argv.index("--system-prompt") + 1] == "SYSTEM RULES"
    assert argv[-1] == "USER TASK"
    # Claude-only output-token cap still applied
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" in captured["env"]


def test_generic_bridge_appends_prompt_to_command(captured):
    bridge = GenericCliBridge(command_line="echo --model foo", timeout=5)

    bridge.generate("SYSTEM RULES", "USER TASK")

    assert captured["argv"][:3] == ["echo", "--model", "foo"]
    assert "USER TASK" in captured["argv"][3]


def test_factory_dispatches_by_provider():
    assert isinstance(create_bridge("claude", cmd="echo"), ClaudeCodeBridge)
    assert isinstance(create_bridge("copilot", cmd="echo"), CopilotCliBridge)
    assert isinstance(create_bridge("gemini", cmd="echo"), GeminiCliBridge)
    assert isinstance(
        create_bridge("custom", cmd="echo --x"), GenericCliBridge
    )


def test_factory_rejects_unknown_provider():
    with pytest.raises(LLMBridgeError):
        create_bridge("chatgpt", cmd="echo")


def test_factory_custom_requires_cmd():
    with pytest.raises(LLMBridgeError):
        create_bridge("custom")


def test_missing_cli_gives_install_hint():
    with pytest.raises(LLMBridgeError) as exc:
        CopilotCliBridge(cmd="definitely-not-a-real-binary-xyz")
    assert "npm install -g @github/copilot" in str(exc.value)


def test_known_providers_matches_cli_choices():
    assert KNOWN_PROVIDERS == ("claude", "copilot", "gemini", "custom")


def test_session_limit_output_raises_session_limit_error(monkeypatch):
    """A CLI exit reporting the usage/session limit must raise the
    distinct LLMSessionLimitError so the pipeline aborts instead of
    retrying doomed calls."""
    from test_automator.utils.exceptions import LLMSessionLimitError

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1,
            stdout="You've hit your session limit · resets 4:50pm",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    bridge = CopilotCliBridge(cmd="echo", timeout=5)

    with pytest.raises(LLMSessionLimitError):
        bridge.generate("sys", "user")


def test_session_limit_error_is_a_bridge_error(monkeypatch):
    """It subclasses LLMBridgeError so existing broad handlers still
    catch it, but its type lets the pipeline treat it specially."""
    from test_automator.utils.exceptions import LLMSessionLimitError

    assert issubclass(LLMSessionLimitError, LLMBridgeError)


def test_calls_made_counter_increments(captured):
    bridge = CopilotCliBridge(cmd="echo", timeout=5)
    assert bridge.calls_made == 0
    bridge.generate("s", "u")
    bridge.generate("s", "u")
    assert bridge.calls_made == 2


def test_usage_summary_tracks_calls_and_tokens(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="x" * 400, stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    bridge = CopilotCliBridge(cmd="echo", timeout=5)
    assert "0 LLM call" in bridge.usage_summary()
    bridge.generate("s" * 800, "u" * 800)  # ~1600 input chars, 400 output
    summary = bridge.usage_summary()
    assert "1 LLM call" in summary
    # ~400 input tokens (1600/4) + ~100 output tokens (400/4)
    assert "prompt" in summary and "response" in summary


def test_claude_json_output_yields_real_tokens_and_cost(monkeypatch):
    import json as _json
    from test_automator.llm_bridge import ClaudeCodeBridge

    payload = _json.dumps({
        "result": "def test_x():\n    assert True\n",
        "usage": {
            "input_tokens": 10,
            "cache_creation_input_tokens": 4000,
            "cache_read_input_tokens": 2000,
            "output_tokens": 500,
        },
        "total_cost_usd": 0.0157,
        "is_error": False,
    })

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    bridge = ClaudeCodeBridge(cmd="echo", timeout=5)
    text = bridge.generate("sys", "user")
    assert text == "def test_x():\n    assert True\n"  # unwrapped result

    summary = bridge.usage_summary()
    assert "$0.0157" in summary
    assert "out tokens" in summary
    assert "estimated" not in summary  # real figures, not the fallback


def test_claude_falls_back_to_text_when_not_json(monkeypatch):
    from test_automator.llm_bridge import ClaudeCodeBridge

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout="plain non-json text", stderr=""
        )
    monkeypatch.setattr(subprocess, "run", fake_run)

    bridge = ClaudeCodeBridge(cmd="echo", timeout=5)
    assert bridge.generate("s", "u") == "plain non-json text"
    # no real usage → estimate summary
    assert "estimated" in bridge.usage_summary()


def test_json_in_band_session_limit_aborts(monkeypatch):
    import json as _json
    from test_automator.llm_bridge import ClaudeCodeBridge
    from test_automator.utils.exceptions import LLMSessionLimitError

    payload = _json.dumps({
        "result": "You've hit your session limit · resets 3:40pm",
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "is_error": True,
    })

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    bridge = ClaudeCodeBridge(cmd="echo", timeout=5)
    with pytest.raises(LLMSessionLimitError):
        bridge.generate("s", "u")
