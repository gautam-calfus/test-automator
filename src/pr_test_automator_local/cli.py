"""Command-line entry point: ``pr-test-automator-local``."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from pr_test_automator_local.config import LocalTestConfig
from pr_test_automator_local.models import PipelineResult
from pr_test_automator_local.orchestrator import LocalTestPipeline


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


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pr-test-automator-local",
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
        help="Times to ask Claude to fix failing tests (default: 3).",
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
        "--claude-code-cmd",
        default="claude",
        help="Claude Code CLI command (default: claude).",
    )
    p.add_argument(
        "--claude-code-timeout",
        type=int,
        default=180,
        help="Timeout in seconds for each Claude Code call (default: 180).",
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

    config = LocalTestConfig(
        repo_path=repo_path,
        base_branch=args.base_branch,
        test_dirs=test_dirs,
        source_root=args.source_root,
        max_fix_retries=args.max_fix_retries,
        commit_tests=commit_tests,
        commit_only_if_passing=not args.commit_on_failure,
        push=push,
        open_pr=args.open_pr,
        claude_code_cmd=args.claude_code_cmd,
        claude_code_timeout=args.claude_code_timeout,
    )

    print(f"Running pr-test-automator-local in {repo_path}")
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