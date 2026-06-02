"""Subprocess bridge to Claude Code.

Wraps the ``claude`` CLI so the rest of the pipeline can ask for code
generation/fixing without caring whether the LLM is a paid API, a local
model, or an editor-integrated assistant.

For the user: install Claude Code first, sign in to your Anthropic account,
and make sure ``claude --version`` works in your shell. Then this bridge
shells out to ``claude --print`` for one-shot non-interactive prompts.

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
    """Invokes the Claude Code CLI via ``claude --print``.

    Requires:
        - ``claude`` on PATH
        - You're signed in (run ``claude`` once interactively and complete
          the OAuth flow if you haven't)
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
        """Run Claude Code in non-interactive print mode.

        We combine system + user prompts into a single message because the
        ``--print`` mode takes one prompt. We mark the system section
        clearly so Claude follows the constraints.
        """
        combined = (
            f"<system_instructions>\n{system_prompt}\n</system_instructions>\n"
            f"\n{user_prompt}"
        )

        logger.info("invoking claude code", extra={"chars": len(combined)})

        try:
            proc = subprocess.run(
                [self._cmd, "--print", combined],
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
            raise LLMBridgeError(
                f"Claude Code returned exit code {proc.returncode}:\n"
                f"stdout: {proc.stdout[:500]}\n"
                f"stderr: {proc.stderr[:500]}"
            )

        return proc.stdout
