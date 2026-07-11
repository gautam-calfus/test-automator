"""Custom exceptions."""

from __future__ import annotations


class LocalTestAutomatorError(Exception):
    """Base for all errors raised by the local automator."""

    def __init__(self, message: str, step: str = "unknown") -> None:
        self.step = step
        super().__init__(message)


class DiffReaderError(LocalTestAutomatorError):
    def __init__(self, message: str) -> None:
        super().__init__(message, step="diff_reader")


class CodeAnalyzerError(LocalTestAutomatorError):
    def __init__(self, message: str) -> None:
        super().__init__(message, step="code_analyzer")


class TestFinderError(LocalTestAutomatorError):
    def __init__(self, message: str) -> None:
        super().__init__(message, step="test_finder")


class TestGeneratorError(LocalTestAutomatorError):
    def __init__(self, message: str) -> None:
        super().__init__(message, step="test_generator")


class TestRunnerError(LocalTestAutomatorError):
    def __init__(self, message: str) -> None:
        super().__init__(message, step="test_runner")


class FailureFixerError(LocalTestAutomatorError):
    def __init__(self, message: str) -> None:
        super().__init__(message, step="failure_fixer")


class TestCommitterError(LocalTestAutomatorError):
    def __init__(self, message: str) -> None:
        super().__init__(message, step="test_committer")


class LLMBridgeError(LocalTestAutomatorError):
    """Raised when the Claude Code subprocess fails."""

    def __init__(self, message: str) -> None:
        super().__init__(message, step="llm_bridge")


class LLMSessionLimitError(LLMBridgeError):
    """Raised when the LLM CLI reports the usage/session limit is hit.

    Distinct from a generic bridge failure so the pipeline can ABORT
    the whole run immediately — every further call would fail the same
    way — instead of burning attempts on doomed calls, while keeping
    the tests already generated and passing.
    """
