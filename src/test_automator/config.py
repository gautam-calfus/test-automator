"""Configuration for the local test automator."""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_TEST_DIRS: tuple[str, ...] = ("tests", "test")
DEFAULT_BOT_NAME = "test-automator[bot]"
DEFAULT_BOT_EMAIL = "test-automator[bot]@users.noreply.github.com"
DEFAULT_MAX_FIX_RETRIES = 2
# Reasoning effort for Claude Code calls. The interactive app defaults
# a subscription session to "high", which our headless one-shot calls
# would otherwise inherit — high effort spends large reasoning-token
# budgets, making each generate/fix call run many minutes and burn
# quota fast. Test generation is a fairly mechanical task, so "low" is
# a big token/time saving with little quality loss; raise via --effort
# for tricky code. (low | medium | high | xhigh | max)
DEFAULT_CLAUDE_EFFORT = "low"
# Optional cap on changed functions processed per file. Default 0 =
# UNLIMITED: cover everything that changed (thorough coverage is the
# whole point). A user who wants to bound a single huge file can set
# --max-functions-per-file; beyond the cap the extra functions are
# skipped with a clear log line suggesting --file to target them.
DEFAULT_MAX_FUNCTIONS_PER_FILE = 0
DEFAULT_CLAUDE_CODE_CMD = "claude"
DEFAULT_CLAUDE_CODE_TIMEOUT = 180
# Output-token cap per LLM call. This is the dominant token/quota
# sink: on a subscription the rolling session limit is driven mostly
# by OUTPUT tokens, and an uncapped generation for a verbose React
# component would run for 10-15 min emitting tens of thousands of
# tokens — a handful of those exhaust the session before a run
# finishes. One test file realistically needs a few thousand tokens;
# 16K leaves generous headroom while bounding worst-case spend and
# per-call time. Batching (>4 functions/file) already splits large
# files across calls, so this rarely truncates. Raise with
# --max-output-tokens for an unusually large single file.
DEFAULT_MAX_OUTPUT_TOKENS = 16_000
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
    repair_existing: bool = False
    """When the pre-flight finds the existing test suite doesn't
    compile, attempt to fix those pre-existing broken test files with
    the LLM (bounded by max_fix_retries) before generating new tests,
    instead of aborting. Off by default — it mutates existing test
    files and spends tokens on code outside your diff. Set via
    --repair-existing."""
    committed_only: bool = False
    """When True, diff ``base...HEAD`` (committed changes only).
    Default False: diff the working tree against the merge-base with
    the base branch, so uncommitted modifications and untracked files
    are analyzed too. Set via --committed-only.
    """
    test_dirs: list[str] = field(
        default_factory=lambda: list(DEFAULT_TEST_DIRS),
    )
    source_root: str | None = None
    max_fix_retries: int = DEFAULT_MAX_FIX_RETRIES
    max_functions_per_file: int = DEFAULT_MAX_FUNCTIONS_PER_FILE
    """Max changed functions to generate tests for per file (0 =
    unlimited). Keeps a single large file from fanning out into many
    LLM calls and an unreviewable pile of tests. Set via
    --max-functions-per-file. Use --file to target specific files when
    a big module's extra functions get skipped."""
    use_cache: bool = True
    """Reuse previously generated tests when the source functions,
    mode, and existing test content are unchanged (content-hash cache).
    Makes repeated runs DETERMINISTIC — same input, same tests — and
    skips the LLM entirely on a hit. Disable with --no-cache to force
    fresh generation every run."""
    commit_tests: bool = False
    commit_only_if_passing: bool = True
    push: bool = False
    open_pr: bool = False
    llm_provider: str = "claude"
    """Which LLM CLI generates the tests: 'claude' (default),
    'copilot' (GitHub Copilot CLI), 'gemini' (Gemini CLI), or
    'custom' (any command via llm_cmd). Set via --llm.
    """
    llm_cmd: str | None = None
    """Override the CLI binary for the chosen provider; for 'custom',
    the full command line to run (prompt appended as last argument).
    Set via --llm-cmd.
    """
    claude_code_cmd: str = DEFAULT_CLAUDE_CODE_CMD
    claude_effort: str = DEFAULT_CLAUDE_EFFORT
    """Reasoning-effort level for Claude Code calls (--effort). See
    DEFAULT_CLAUDE_EFFORT: 'low' keeps per-call token/time down; raise
    for tricky code."""
    claude_code_timeout: int = DEFAULT_CLAUDE_CODE_TIMEOUT
    claude_code_max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    """Output-token cap per Claude Code call, applied via the
    CLAUDE_CODE_MAX_OUTPUT_TOKENS env var. The dominant token/quota
    lever — see DEFAULT_MAX_OUTPUT_TOKENS. A value already set in the
    environment wins.
    """
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