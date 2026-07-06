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
