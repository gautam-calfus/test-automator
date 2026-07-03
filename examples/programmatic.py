"""Programmatic example: run the local pipeline without the CLI."""

from __future__ import annotations

import os

from test_automator import LocalTestConfig, LocalTestPipeline


def main() -> None:
    config = LocalTestConfig(
        repo_path=os.getcwd(),
        base_branch="main",
        test_dirs=["tests"],
        source_root="src",
        max_fix_retries=1,
        commit_tests=False,
        push=False,
        open_pr=False,
    )

    result = LocalTestPipeline(config).run()

    print()
    print(f"Result: {'PASS' if result.is_passing else 'FAIL'}")
    print(f"  Files changed:      {result.files_changed}")
    print(f"  Functions analyzed: {result.functions_affected}")
    print(f"  Tests generated:    {result.tests_generated}")
    if result.test_result:
        r = result.test_result
        print(f"  passed: {r.passed}  failed: {r.failed}  errors: {r.errors}")


if __name__ == "__main__":
    main()
