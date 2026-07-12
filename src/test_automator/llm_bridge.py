"""Subprocess bridges to LLM CLIs (Claude Code, Copilot CLI, Gemini CLI).

The rest of the pipeline only calls ``LLMBridge.generate(system_prompt,
user_prompt)`` — it never cares which model or CLI produced the text.
This module provides one bridge per supported CLI plus a generic bridge
for anything else, selected via ``--llm`` on the command line:

- ``claude``  (default) → ``ClaudeCodeBridge``  — Claude Code
- ``copilot``           → ``CopilotCliBridge``  — GitHub Copilot CLI
- ``gemini``            → ``GeminiCliBridge``   — Google Gemini CLI
- ``custom``            → ``GenericCliBridge``  — any command via --llm-cmd

== Per-CLI invocation notes ==

**Claude Code** supports true system prompts and tool disabling, so it
gets the most controlled invocation (see ClaudeCodeBridge below). The
naive invocation treats Claude Code as a plain completion endpoint; it
isn't — it's an agentic harness with file-modifying tools. We force
agent-free single-response behavior with:

- ``--tools ""``: disables ALL built-in tools.
- ``--system-prompt <prompt>``: replaces the default agent system
  prompt with OUR prompt (the language style guide).
- ``--output-format text``: plain text, not JSON wrapped.
- ``--permission-mode bypassPermissions``: belt-and-suspenders.

These flags were verified against Claude Code 2.1.x.

**Copilot CLI** (``copilot -p``) and **Gemini CLI** (``gemini -p``) run
headless: the prompt is passed with ``-p`` and the response is printed
to stdout. Neither exposes a separate system-prompt flag, so the system
prompt is prepended to the user prompt. Neither is granted tool
permissions (no ``--allow-all-tools`` / ``--yolo``), so tool calls are
denied and the model falls back to text-only output — which is exactly
what we want.

**Generic** wraps any CLI that takes a prompt as its final argument and
prints the response to stdout: ``--llm custom --llm-cmd "mycli --flag"``.

All bridges rely on the language extractors being defensive: they strip
markdown fences and prose regardless of which model produced the output.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from typing import Protocol

from test_automator._logging import get_logger
from test_automator.utils.exceptions import (
    LLMBridgeError,
    LLMSessionLimitError,
)

logger = get_logger(__name__)

# Substrings that mean the LLM CLI's usage/session quota is exhausted.
# When any appears, further calls are pointless until the quota resets,
# so the bridge raises LLMSessionLimitError and the pipeline aborts.
_SESSION_LIMIT_MARKERS = (
    "session limit",
    "usage limit",
    "rate limit",
    "quota",
    "resets ",
    "reset at",
    "too many requests",
)


def _is_session_limit(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _SESSION_LIMIT_MARKERS)


class LLMBridge(Protocol):
    """Interface for any LLM backend. Implement this to swap models."""

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        """Send prompts to the LLM and return its text response."""
        ...


def _combine_prompts(system_prompt: str, user_prompt: str) -> str:
    """For CLIs without a system-prompt flag, fold the system prompt
    into the user prompt. The system prompts already carry strict
    output-format instructions, which survive this flattening fine.
    """
    return (
        f"<instructions>\n{system_prompt}\n</instructions>\n\n{user_prompt}"
    )


class _CliBridge:
    """Shared subprocess plumbing: availability check, timeout, and
    error reporting. Subclasses define the argv (and optionally env)
    for their CLI.
    """

    provider = "generic"

    def __init__(self, cmd: str, timeout: int = 180) -> None:
        self._cmd = cmd
        self._timeout = timeout
        # Running usage counters, surfaced in per-file progress logs so
        # the developer can see how fast a run is spending quota and
        # decide to stop (completed passing files are already on disk).
        # The subscription CLI exposes no queryable "remaining" quota,
        # so cumulative usage-so-far is the actionable signal.
        self.calls_made = 0
        self._input_chars = 0
        self._output_chars = 0
        self._verify_available()

    def usage_summary(self) -> str:
        """One-line cumulative usage: calls + rough token estimate
        (~4 chars/token, split into prompt vs response)."""
        in_tok = self._input_chars // 4
        out_tok = self._output_chars // 4

        def k(n: int) -> str:
            return f"{n / 1000:.0f}k" if n >= 1000 else str(n)

        return (
            f"{self.calls_made} LLM call(s), ~{k(in_tok + out_tok)} tokens "
            f"(prompt ~{k(in_tok)} / response ~{k(out_tok)})"
        )

    # -- hooks ---------------------------------------------------------

    def _argv(self, system_prompt: str, user_prompt: str) -> list[str]:
        raise NotImplementedError

    def _env(self) -> dict[str, str] | None:
        """Environment for the subprocess. None inherits the parent's."""
        return None

    def _install_hint(self) -> str:
        return f"Install the CLI providing `{self._cmd}` and sign in."

    def _flag_error_hint(self) -> str:
        return (
            f"The `{self._cmd}` CLI rejected one of our flags — its "
            f"installed version may be too old or too new for this bridge."
        )

    # -- shared behavior -------------------------------------------------

    def _verify_available(self) -> None:
        """Fail early with a helpful message if the CLI isn't installed."""
        if shutil.which(self._cmd) is None:
            raise LLMBridgeError(
                f"`{self._cmd}` not found on PATH. {self._install_hint()}"
            )

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls_made += 1
        self._input_chars += len(system_prompt) + len(user_prompt)
        logger.info(
            "invoking %s cli",
            self.provider,
            extra={
                "chars": len(system_prompt) + len(user_prompt),
                "call": self.calls_made,
            },
        )

        cmd = self._argv(system_prompt, user_prompt)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
                env=self._env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMBridgeError(
                f"{self.provider} CLI timed out after {self._timeout}s. "
                "Increase claude_code_timeout in config or simplify the diff."
            ) from exc
        except FileNotFoundError as exc:
            raise LLMBridgeError(
                f"`{self._cmd}` disappeared mid-run: {exc}"
            ) from exc

        if proc.returncode != 0:
            err = (proc.stderr or "") + (proc.stdout or "")
            if _is_session_limit(err):
                raise LLMSessionLimitError(
                    f"{self.provider} CLI usage/session limit reached — "
                    f"aborting the run to avoid wasted calls. Tests "
                    f"already generated and passing are kept. Detail: "
                    f"{err.strip()[:200]}"
                )
            if "unknown option" in err.lower() or "unrecognized" in err.lower():
                raise LLMBridgeError(
                    f"{self._flag_error_hint()}\n"
                    f"Underlying error:\nstdout: {proc.stdout[:300]}\n"
                    f"stderr: {proc.stderr[:300]}"
                )
            raise LLMBridgeError(
                f"{self.provider} CLI returned exit code {proc.returncode}:\n"
                f"stdout: {proc.stdout[:500]}\n"
                f"stderr: {proc.stderr[:500]}"
            )

        self._output_chars += len(proc.stdout)
        return proc.stdout


class ClaudeCodeBridge(_CliBridge):
    """Invokes the Claude Code CLI via ``claude --print`` with
    agent-disabling flags (see module docstring).

    Requires ``claude`` 2.1.x or newer on PATH, signed in.
    """

    provider = "claude"

    def __init__(
        self,
        cmd: str = "claude",
        timeout: int = 180,
        max_output_tokens: int = 16_000,
    ) -> None:
        self._max_output_tokens = max_output_tokens
        super().__init__(cmd=cmd, timeout=timeout)

    def _argv(self, system_prompt: str, user_prompt: str) -> list[str]:
        return [
            self._cmd,
            "--print",
            "--output-format", "text",
            "--tools", "",
            "--system-prompt", system_prompt,
            "--permission-mode", "bypassPermissions",
            user_prompt,
        ]

    def _env(self) -> dict[str, str]:
        # Claude Code caps responses at 32K output tokens by default.
        # A full JUnit test file for a service with several changed
        # methods routinely exceeds that (real failure: Acme's
        # QuestionRoutingService, 7 changed methods — three runs died
        # on "response exceeded the 32000 output token maximum").
        # Raise the cap via env var; an explicit value already present
        # in the environment wins over our default.
        env = os.environ.copy()
        env.setdefault(
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS", str(self._max_output_tokens)
        )
        return env

    def _install_hint(self) -> str:
        return (
            "Install Claude Code:\n"
            "  npm install -g @anthropic-ai/claude-code\n"
            f"Then sign in by running `{self._cmd}` once and completing "
            "the OAuth prompt."
        )

    def _flag_error_hint(self) -> str:
        return (
            "Claude Code rejected one of our flags. This bridge requires "
            "Claude Code 2.1.x or newer (for --tools and --system-prompt). "
            "Upgrade with:\n  npm install -g @anthropic-ai/claude-code@latest"
        )


class CopilotCliBridge(_CliBridge):
    """Invokes the GitHub Copilot CLI in programmatic mode:
    ``copilot -p "<prompt>"``.

    No tool-permission flags are passed, so Copilot cannot modify files
    or run commands — tool requests are denied and it answers in text.
    The system prompt is folded into the prompt (no system-prompt flag).

    Requires the Copilot CLI (``npm install -g @github/copilot``) and
    an authenticated GitHub account with a Copilot subscription.
    """

    provider = "copilot"

    def __init__(self, cmd: str = "copilot", timeout: int = 180) -> None:
        super().__init__(cmd=cmd, timeout=timeout)

    def _argv(self, system_prompt: str, user_prompt: str) -> list[str]:
        return [
            self._cmd,
            "-p", _combine_prompts(system_prompt, user_prompt),
        ]

    def _install_hint(self) -> str:
        return (
            "Install the GitHub Copilot CLI:\n"
            "  npm install -g @github/copilot\n"
            f"Then run `{self._cmd}` once to authenticate with GitHub."
        )


class GeminiCliBridge(_CliBridge):
    """Invokes the Google Gemini CLI in headless mode:
    ``gemini -p "<prompt>"``.

    No ``--yolo`` flag is passed, so Gemini cannot auto-run tools; it
    answers in text. The system prompt is folded into the prompt.

    Requires the Gemini CLI (``npm install -g @google/gemini-cli``),
    authenticated (Google login or GEMINI_API_KEY).
    """

    provider = "gemini"

    def __init__(self, cmd: str = "gemini", timeout: int = 180) -> None:
        super().__init__(cmd=cmd, timeout=timeout)

    def _argv(self, system_prompt: str, user_prompt: str) -> list[str]:
        return [
            self._cmd,
            "-p", _combine_prompts(system_prompt, user_prompt),
        ]

    def _install_hint(self) -> str:
        return (
            "Install the Gemini CLI:\n"
            "  npm install -g @google/gemini-cli\n"
            f"Then run `{self._cmd}` once to authenticate."
        )


class GenericCliBridge(_CliBridge):
    """Wraps ANY CLI that accepts the prompt as its final argument and
    prints the model's response to stdout::

        test-automator --llm custom --llm-cmd "mycli --model foo -p"

    The command string is shell-split; the combined system+user prompt
    is appended as the last argument.
    """

    provider = "custom"

    def __init__(self, command_line: str, timeout: int = 180) -> None:
        argv = shlex.split(command_line)
        if not argv:
            raise LLMBridgeError(
                "--llm-cmd is empty — pass the CLI command to run, e.g. "
                '--llm-cmd "mycli --flag"'
            )
        self._argv_prefix = argv
        super().__init__(cmd=argv[0], timeout=timeout)

    def _argv(self, system_prompt: str, user_prompt: str) -> list[str]:
        return self._argv_prefix + [
            _combine_prompts(system_prompt, user_prompt)
        ]


#: Providers accepted by --llm / config.llm_provider.
KNOWN_PROVIDERS = ("claude", "copilot", "gemini", "custom")


def create_bridge(
    provider: str = "claude",
    cmd: str | None = None,
    timeout: int = 180,
    max_output_tokens: int = 16_000,
) -> LLMBridge:
    """Build the right bridge for ``provider``.

    Args:
        provider: one of KNOWN_PROVIDERS.
        cmd: override the CLI binary (or, for ``custom``, the full
            command line to run — required in that case).
        timeout: seconds per LLM call (all providers).
        max_output_tokens: Claude-only response cap (ignored by others,
            which have no equivalent knob).
    """
    if provider == "claude":
        return ClaudeCodeBridge(
            cmd=cmd or "claude",
            timeout=timeout,
            max_output_tokens=max_output_tokens,
        )
    if provider == "copilot":
        return CopilotCliBridge(cmd=cmd or "copilot", timeout=timeout)
    if provider == "gemini":
        return GeminiCliBridge(cmd=cmd or "gemini", timeout=timeout)
    if provider == "custom":
        if not cmd:
            raise LLMBridgeError(
                "--llm custom requires --llm-cmd with the command to run, "
                'e.g. --llm custom --llm-cmd "mycli --flag"'
            )
        return GenericCliBridge(command_line=cmd, timeout=timeout)
    raise LLMBridgeError(
        f"Unknown LLM provider {provider!r} — expected one of "
        f"{', '.join(KNOWN_PROVIDERS)}"
    )
