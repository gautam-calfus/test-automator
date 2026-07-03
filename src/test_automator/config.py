"""Configuration for the local test automator."""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_TEST_DIRS: tuple[str, ...] = ("tests", "test")
DEFAULT_BOT_NAME = "test-automator[bot]"
DEFAULT_BOT_EMAIL = "test-automator[bot]@users.noreply.github.com"
DEFAULT_MAX_FIX_RETRIES = 3
DEFAULT_CLAUDE_CODE_CMD = "claude"
DEFAULT_CLAUDE_CODE_TIMEOUT = 180
DEFAULT_TEST_RUNNER_TIMEOUT = 600


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
        test_runner_timeout:   Seconds to wait for the test runner subprocess
                               (Gradle for Kotlin, pytest for Python) to
                               complete. Bumped from 120s in earlier releases
                               because Gradle cold-starts and large compile
                               steps can exceed two minutes on real codebases.
        bot_name:              Git author for the commit.
        bot_email:             Git email for the commit.
        languages:             Iterable of language names to enable (default:
                               None means all registered languages — which in
                               v0.2.0 means just Python). Set to ``["python"]``
                               explicitly to opt out of future auto-enabled
                               languages.
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
    test_runner_timeout: int = DEFAULT_TEST_RUNNER_TIMEOUT
    bot_name: str = DEFAULT_BOT_NAME
    bot_email: str = DEFAULT_BOT_EMAIL
    languages: list[str] | None = None
    java_file_filter: list[str] | None = None
    """Java-specific file categories to generate tests for. None means
    all Java files. Valid values: 'services', 'controllers', 'daos',
    'handlers'. Set via --java-file-filter CLI flag.
    """
    file_whitelist: list[str] | None = None
    """If set, ONLY process files in this list (repo-relative paths).
    All other files are dropped. Set via --file CLI flag (repeatable).
    Overrides java_file_filter.
    """

    @property
    def all_test_dirs(self) -> list[str]:
        return list(self.test_dirs)