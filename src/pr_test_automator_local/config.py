"""Configuration for the local test automator."""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_TEST_DIRS: tuple[str, ...] = ("tests", "test")
DEFAULT_BOT_NAME = "pr-test-automator[bot]"
DEFAULT_BOT_EMAIL = "pr-test-automator[bot]@users.noreply.github.com"
DEFAULT_MAX_FIX_RETRIES = 3
DEFAULT_CLAUDE_CODE_CMD = "claude"
DEFAULT_CLAUDE_CODE_TIMEOUT = 180


@dataclass
class LocalTestConfig:
    """Settings for a local test-generation run.

    Required:
        repo_path:        Absolute local path to the repo root.

    Optional:
        base_branch:           Branch to diff against (default: 'main').
        test_dirs:             Test directory search paths (priority order).
        source_root:           Restrict analysis to files under this path.
        max_fix_retries:       Times to ask Claude to fix failing tests.
        commit_tests:          Commit generated tests after writing.
        commit_only_if_passing: When True (default), skip the commit when any
                               test fails. When False, commit regardless.
        push:                  Push the commit to the current branch's remote.
        open_pr:               Open a PR via `gh` CLI after pushing.
        claude_code_cmd:       Command to invoke Claude Code (default: 'claude').
        claude_code_timeout:   Seconds to wait for each Claude Code response.
        bot_name:              Git author for the commit.
        bot_email:             Git email for the commit.
    """

    repo_path: str
    base_branch: str = "main"
    test_dirs: list[str] = field(
        default_factory=lambda: list(DEFAULT_TEST_DIRS),
    )
    source_root: str | None = None
    max_fix_retries: int = DEFAULT_MAX_FIX_RETRIES
    commit_tests: bool = False
    commit_only_if_passing: bool = True
    push: bool = False
    open_pr: bool = False
    claude_code_cmd: str = DEFAULT_CLAUDE_CODE_CMD
    claude_code_timeout: int = DEFAULT_CLAUDE_CODE_TIMEOUT
    bot_name: str = DEFAULT_BOT_NAME
    bot_email: str = DEFAULT_BOT_EMAIL

    @property
    def all_test_dirs(self) -> list[str]:
        return list(self.test_dirs)