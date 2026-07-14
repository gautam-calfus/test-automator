"""Command-line entry point: ``test-automator``."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from test_automator.config import LocalTestConfig
from test_automator.models import PipelineResult
from test_automator.orchestrator import LocalTestPipeline


def _find_git_root(start: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return result.stdout.strip()
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return None


def _parse_file_whitelist(raw: list[str] | None) -> list[str] | None:
    """Flatten --file values into a de-duplicated path list.

    ``--file`` is ``action="append"``, so ``raw`` is a list of the
    strings given. Each may itself be a comma-separated list, so both
    ``--file a --file b`` and ``--file a,b`` (and a mix) produce the
    same result. Blank entries and surrounding whitespace are dropped;
    order is preserved. Returns None when nothing usable was given.
    """
    if not raw:
        return None
    out: list[str] = []
    for entry in raw:
        for part in entry.split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out or None


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="test-automator",
        description=(
            "Generate pytest tests for changed Python functions on your "
            "current branch using Claude Code. Run this from inside a git "
            "repo with uncommitted/committed changes since the base branch."
        ),
    )
    p.add_argument(
        "--repo-path",
        default=None,
        help="Path to repo root (default: detect via `git rev-parse`).",
    )
    p.add_argument(
        "--base-branch",
        default="main",
        help="Branch to diff against (default: main).",
    )
    p.add_argument(
        "--repair-existing",
        action="store_true",
        help=(
            "If the existing test suite doesn't compile (pre-flight), "
            "try to fix the broken existing test files with the LLM "
            "before generating new tests, instead of aborting. Mutates "
            "existing test files and spends extra tokens; off by "
            "default."
        ),
    )
    p.add_argument(
        "--no-fetch",
        action="store_true",
        help=(
            "Don't auto-fetch when --base-branch is a remote ref "
            "(origin/develop). By default such a base is fetched first "
            "so the diff reflects the live remote, not a stale local "
            "cache."
        ),
    )
    p.add_argument(
        "--committed-only",
        action="store_true",
        help=(
            "Diff committed changes only (git diff base...HEAD). By "
            "default the working tree is diffed against the merge-base "
            "with the base branch, so uncommitted and untracked "
            "changes are analyzed too."
        ),
    )
    p.add_argument(
        "--test-dirs",
        default="tests",
        help="Comma-separated test dirs, priority order (default: tests).",
    )
    p.add_argument(
        "--source-root",
        default=None,
        help="Restrict analysis to files under this path (e.g. 'src').",
    )
    p.add_argument(
        "--max-fix-retries",
        type=int,
        default=3,
        help=(
            "Times to ask the LLM to fix failing tests (default: 3). "
            "The first fix lands most real repairs; extra attempts on a "
            "stubborn file mostly burn quota, so keep this low."
        ),
    )
    p.add_argument(
        "--effort",
        choices=["low", "medium", "high", "xhigh", "max"],
        default="low",
        help=(
            "Reasoning effort for Claude Code calls (default: low). "
            "Higher effort spends far more reasoning tokens per call "
            "(minutes-long calls, faster quota burn); test generation "
            "rarely needs it. Raise to medium/high only for tricky code."
        ),
    )
    p.add_argument(
        "--no-cache",
        action="store_true",
        help=(
            "Disable the generated-test cache and force fresh LLM "
            "generation. By default, unchanged code reuses cached "
            "tests so repeated runs are deterministic and cheap."
        ),
    )
    p.add_argument(
        "--max-functions-per-file",
        type=int,
        default=0,
        help=(
            "Optional cap on changed functions per file (default: 0 = "
            "unlimited, cover everything). Set a limit only to bound a "
            "single very large file; skipped functions are logged so "
            "you can target them with --file."
        ),
    )
    p.add_argument(
        "--commit-tests",
        action="store_true",
        help=(
            "Commit generated tests after writing. By default, the commit "
            "is skipped if any tests fail; use --commit-on-failure to force."
        ),
    )
    p.add_argument(
        "--commit-on-failure",
        action="store_true",
        help=(
            "Commit even when generated tests don't all pass. Has no effect "
            "unless --commit-tests (or --push / --open-pr) is also set."
        ),
    )
    p.add_argument(
        "--push",
        action="store_true",
        help="Push the commit to the current branch (implies --commit-tests).",
    )
    p.add_argument(
        "--open-pr",
        action="store_true",
        help="Open a PR via the `gh` CLI (implies --push).",
    )
    p.add_argument(
        "--llm",
        choices=["claude", "copilot", "gemini", "custom"],
        default="claude",
        help=(
            "Which LLM CLI generates the tests (default: claude). "
            "'copilot' uses the GitHub Copilot CLI (`copilot -p`), "
            "'gemini' uses the Gemini CLI (`gemini -p`), 'custom' runs "
            "the command given by --llm-cmd with the prompt appended."
        ),
    )
    p.add_argument(
        "--llm-cmd",
        default=None,
        help=(
            "Override the CLI binary for the chosen --llm provider "
            "(e.g. --llm gemini --llm-cmd /opt/bin/gemini). For "
            "--llm custom, the full command line to run, e.g. "
            '--llm custom --llm-cmd "mycli --model foo".'
        ),
    )
    p.add_argument(
        "--claude-code-cmd",
        default="claude",
        help=(
            "Claude Code CLI command (default: claude). Only used with "
            "--llm claude; prefer --llm-cmd for other providers."
        ),
    )
    p.add_argument(
        "--claude-code-timeout", "--llm-timeout",
        type=int,
        default=180,
        help=(
            "Timeout in seconds for each LLM CLI call, regardless of "
            "provider (default: 180)."
        ),
    )
    p.add_argument(
        "--max-output-tokens",
        type=int,
        default=16_000,
        help=(
            "Output-token cap for each Claude Code call, applied via the "
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS env var (default: 16000). This "
            "is the main token/quota lever: output tokens dominate the "
            "subscription session limit, so an uncapped generation can "
            "run for many minutes and burn the quota. 16000 is ample for "
            "one test file; raise it only for an unusually large file. A "
            "value already set in your environment wins."
        ),
    )
    p.add_argument(
        "--test-runner-timeout",
        type=int,
        default=600,
        help=(
            "Timeout in seconds for the test runner subprocess (Gradle/"
            "pytest). Default 600s (10 min). Bump higher for slow Gradle "
            "cold starts or huge test suites."
        ),
    )
    p.add_argument(
        "--java-file-filter",
        default=None,
        help=(
            "Comma-separated categories of Java files to generate tests "
            "for. Others are analyzed but skipped at the test-generation "
            "step (saves LLM quota). Values: 'services', 'controllers', "
            "'daos', 'handlers', 'all'. Multiple values: "
            "--java-file-filter services,controllers. When unset (default), "
            "all Java files eligible for tests are processed."
        ),
    )
    p.add_argument(
        "--file",
        default=None,
        action="append",
        help=(
            "Process ONLY the specified file(s), path(s) relative to "
            "repo root. Two ways to name several: repeat the flag "
            "(--file a.java --file b.java) OR pass a comma-separated "
            "list (--file a.java,b.java). The two can be combined. "
            "Bypasses --java-file-filter (if you name files, we assume "
            "you want them tested). Useful for scoping a run to a few "
            "files without spending quota on unchanged neighbors. "
            "Example: --file src/main/java/com/acme/service/CMService.java"
        ),
    )
    return p


def _print_summary(result: PipelineResult) -> None:
    status = "PASS ✓" if result.is_passing else "FAIL ✗"
    print()
    print("=" * 60)
    print(f"  Result             : {status}")
    print(f"  Branch             : {result.head_branch} -> {result.base_branch}")
    print(f"  Files changed      : {result.files_changed}")
    print(f"  Functions analyzed : {result.functions_affected}")
    print(f"  Tests generated    : {result.tests_generated}")
    if result.test_result:
        r = result.test_result
        print(f"  Tests passed       : {r.passed}")
        print(f"  Tests failed       : {r.failed}")
        print(f"  Tests errored      : {r.errors}")
    if result.llm_usage:
        print(f"  LLM usage          : {result.llm_usage}")
    if result.commit_sha:
        print(f"  Commit SHA         : {result.commit_sha}")
    if result.pr_url:
        print(f"  PR URL             : {result.pr_url}")
    print("=" * 60)
    print()
    print("Steps:")
    for step in result.steps:
        icon = "✓" if step.success else "✗"
        print(f"  {icon} {step.step}: {step.message}")
    print()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    repo_path = args.repo_path or _find_git_root(os.getcwd())
    if not repo_path:
        print(
            "ERROR: not inside a git repository. Run this from your project "
            "directory or pass --repo-path.",
            file=sys.stderr,
        )
        return 2

    # Cascade: open-pr implies push implies commit-tests
    commit_tests = args.commit_tests or args.push or args.open_pr
    push = args.push or args.open_pr

    test_dirs = [
        d.strip() for d in args.test_dirs.split(",") if d.strip()
    ] or ["tests"]

    # Parse and validate --java-file-filter
    java_file_filter: list[str] | None = None
    if args.java_file_filter:
        VALID = {"services", "controllers", "daos", "handlers", "all"}
        raw = [
            v.strip().lower()
            for v in args.java_file_filter.split(",")
            if v.strip()
        ]
        invalid = [v for v in raw if v not in VALID]
        if invalid:
            print(
                f"error: --java-file-filter got unknown value(s): "
                f"{', '.join(invalid)}. Valid: {', '.join(sorted(VALID))}"
            )
            return 2
        # 'all' means no filter — treat as None
        if "all" in raw or not raw:
            java_file_filter = None
        else:
            java_file_filter = raw

    config = LocalTestConfig(
        repo_path=repo_path,
        base_branch=args.base_branch,
        committed_only=args.committed_only,
        fetch_base=not args.no_fetch,
        repair_existing=args.repair_existing,
        test_dirs=test_dirs,
        source_root=args.source_root,
        max_fix_retries=args.max_fix_retries,
        max_functions_per_file=args.max_functions_per_file,
        use_cache=not args.no_cache,
        commit_tests=commit_tests,
        commit_only_if_passing=not args.commit_on_failure,
        push=push,
        open_pr=args.open_pr,
        llm_provider=args.llm,
        llm_cmd=args.llm_cmd,
        claude_code_cmd=args.claude_code_cmd,
        claude_effort=args.effort,
        claude_code_timeout=args.claude_code_timeout,
        claude_code_max_output_tokens=args.max_output_tokens,
        test_runner_timeout=args.test_runner_timeout,
        java_file_filter=java_file_filter,
        file_whitelist=_parse_file_whitelist(args.file),
    )

    print(f"Running test-automator in {repo_path}")
    print(
        f"  base_branch={config.base_branch}  "
        f"source_root={config.source_root}"
    )
    print(
        f"  commit={config.commit_tests}  "
        f"commit_only_if_passing={config.commit_only_if_passing}  "
        f"push={config.push}  open_pr={config.open_pr}"
    )
    print(f"  max_fix_retries={config.max_fix_retries}")
    print()

    pipeline = LocalTestPipeline(config)
    result = pipeline.run()
    _print_summary(result)

    return 0 if result.is_passing else 1


if __name__ == "__main__":
    sys.exit(main())