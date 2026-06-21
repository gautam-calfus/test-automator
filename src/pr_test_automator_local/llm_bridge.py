"""Subprocess bridge to Claude Code.

Wraps the ``claude`` CLI so the rest of the pipeline can ask for code
generation/fixing without caring whether the LLM is a paid API, a local
model, or an editor-integrated assistant.

For the user: install Claude Code first, sign in to your Anthropic account,
and make sure ``claude --version`` works in your shell. Then this bridge
shells out to ``claude --print`` for one-shot non-interactive prompts.

== Why we use specific Claude Code flags ==

The naive invocation (``claude --print "<combined prompt>"``) treats
Claude Code as if it were a plain LLM completion endpoint. It isn't —
Claude Code is an agentic harness with file-modifying tools (Write,
Edit, Bash) that the model can call. When given a prompt like "Generate
a Kotlin test file", the agent often tries to USE the Write tool, then
narrates "The file write needs your approval" when no permission UI
is present in a subprocess context. That narration leaks into stdout,
corrupting the LLM output we feed back into the pipeline.

We use these flags to force agent-free single-response behavior:

- ``--tools ""``: disables ALL built-in tools. The agent CANNOT reach
  for Write, Edit, Bash, etc. Forced into text-only response mode.
- ``--system-prompt <prompt>``: replaces Claude Code's default agent
  system prompt with OUR prompt (Strikt + MockK style guide). Without
  this we'd be appending to Claude Code's "you are a coding agent"
  defaults which encourage agentic behavior.
- ``--output-format text``: ensures we get plain text, not JSON wrapped.
- ``--permission-mode bypassPermissions``: belt-and-suspenders — if a
  tool somehow slips through, no permission dialog blocks the run.

These flags were verified against Claude Code 2.1.160 (June 2026).
Older or newer versions may have different flag names — see the
``_verify_flags_supported`` method.

If you'd rather use a different LLM, replace this module — the rest of the
pipeline only calls ``LLMBridge.generate(system_prompt, user_prompt)``.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Protocol

from pr_test_automator_local._logging import get_logger
from pr_test_automator_local.utils.exceptions import LLMBridgeError

logger = get_logger(__name__)


class LLMBridge(Protocol):
    """Interface for any LLM backend. Implement this to swap models."""

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        """Send prompts to the LLM and return its text response."""
        ...


class ClaudeCodeBridge:
    """Invokes the Claude Code CLI via ``claude --print`` with
    agent-disabling flags.

    Requires:
        - ``claude`` 2.1.x or newer on PATH (older versions don't have
          ``--tools`` or ``--system-prompt``)
        - You're signed in (run ``claude`` once interactively and
          complete the OAuth flow if you haven't)
    """

    def __init__(
        self,
        cmd: str = "claude",
        timeout: int = 180,
    ) -> None:
        self._cmd = cmd
        self._timeout = timeout
        self._verify_available()

    def _verify_available(self) -> None:
        """Fail early with a helpful message if Claude Code isn't installed."""
        if shutil.which(self._cmd) is None:
            raise LLMBridgeError(
                f"`{self._cmd}` not found on PATH. Install Claude Code:\n"
                f"  npm install -g @anthropic-ai/claude-code\n"
                f"Then sign in by running `{self._cmd}` once and completing "
                f"the OAuth prompt."
            )

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        """Run Claude Code in non-interactive print mode with tools
        disabled.

        Why each flag matters: see the module docstring. Briefly:
        - ``--tools ""`` disables the agentic harness so Claude can't
          try to write files itself
        - ``--system-prompt`` replaces Claude Code's default with OUR
          style guide
        - ``--output-format text`` plain text (not JSON)
        - ``--permission-mode bypassPermissions`` safety net
        """
        logger.info(
            "invoking claude code",
            extra={"chars": len(system_prompt) + len(user_prompt)},
        )

        cmd = [
            self._cmd,
            "--print",
            "--output-format", "text",
            "--tools", "",
            "--system-prompt", system_prompt,
            "--permission-mode", "bypassPermissions",
            user_prompt,
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMBridgeError(
                f"Claude Code timed out after {self._timeout}s. "
                "Increase claude_code_timeout in config or simplify the diff."
            ) from exc
        except FileNotFoundError as exc:
            raise LLMBridgeError(
                f"`{self._cmd}` disappeared mid-run: {exc}"
            ) from exc

        if proc.returncode != 0:
            # Detect the specific case where flags aren't supported by
            # the installed Claude Code version, and give an actionable
            # error rather than a generic "exit code 1".
            err = (proc.stderr or "") + (proc.stdout or "")
            if "unknown option" in err.lower() or "unrecognized" in err.lower():
                raise LLMBridgeError(
                    "Claude Code rejected one of our flags. This bridge "
                    "requires Claude Code 2.1.x or newer (for --tools and "
                    "--system-prompt). Upgrade with:\n"
                    "  npm install -g @anthropic-ai/claude-code@latest\n"
                    f"Underlying error:\nstdout: {proc.stdout[:300]}\n"
                    f"stderr: {proc.stderr[:300]}"
                )
            raise LLMBridgeError(
                f"Claude Code returned exit code {proc.returncode}:\n"
                f"stdout: {proc.stdout[:500]}\n"
                f"stderr: {proc.stderr[:500]}"
            )

        return proc.stdout
