"""Tests for v0.2 removed-function detection and stale-test pruning.

Real scenario this fixes: a developer deletes (or moves) methods from a
source file. The existing test file still references them, so the test
suite no longer compiles/imports. Pre-v0.2 the bot never looked at the
base version of a file, so it couldn't know anything was removed — the
broken existing tests stayed broken and every run failed identically.

Now the diff reader captures each file's merge-base content, the
analyzer diffs the base function list against the current one, and the
generator prunes tests covering removed functions — mechanically, with
no LLM call.
"""

from __future__ import annotations

import subprocess

from test_automator.config import LocalTestConfig
from test_automator.models import ExistingTest, RemovedFunction
from test_automator.steps.code_analyzer import CodeAnalyzer
from test_automator.steps.local_diff_reader import LocalDiffReader
from test_automator.steps.test_finder import TestFinder
from test_automator.steps.test_generator import TestGenerator


def _init_repo(repo: str) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo,
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo,
                   check=True, capture_output=True)


def _commit_all(repo: str, message: str) -> None:
    subprocess.run(["git", "add", "."], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, check=True,
                   capture_output=True)


def _setup_python_project(repo: str) -> None:
    """main branch: calc.py with foo() and bar(), tests for both."""
    _init_repo(repo)
    (open(f"{repo}/calc.py", "w")).write(
        "def foo():\n    return 1\n\n\ndef bar():\n    return 2\n"
    )
    import os
    os.makedirs(f"{repo}/tests", exist_ok=True)
    (open(f"{repo}/tests/test_calc.py", "w")).write(
        "from calc import foo, bar\n\n\n"
        "def test_foo():\n    assert foo() == 1\n\n\n"
        "def test_bar():\n    assert bar() == 2\n"
    )
    _commit_all(repo, "init")
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo,
                   check=True, capture_output=True)


def test_uncommitted_removal_is_detected(tmp_path) -> None:
    """Deleting bar() in the working tree (no commit) yields a
    RemovedFunction for it.
    """
    repo = str(tmp_path)
    _setup_python_project(repo)

    # Remove bar() — uncommitted
    (open(f"{repo}/calc.py", "w")).write("def foo():\n    return 1\n")

    cfg = LocalTestConfig(repo_path=repo, base_branch="main")
    info = LocalDiffReader(cfg).read()
    removed = CodeAnalyzer(cfg).find_removed(info.files)

    assert [(r.file_path, r.name) for r in removed] == [("calc.py", "bar")]


def test_committed_removal_is_detected(tmp_path) -> None:
    """Same detection when the removal is committed on the branch."""
    repo = str(tmp_path)
    _setup_python_project(repo)

    (open(f"{repo}/calc.py", "w")).write("def foo():\n    return 1\n")
    _commit_all(repo, "remove bar")

    cfg = LocalTestConfig(repo_path=repo, base_branch="main")
    info = LocalDiffReader(cfg).read()
    removed = CodeAnalyzer(cfg).find_removed(info.files)

    assert [(r.file_path, r.name) for r in removed] == [("calc.py", "bar")]


def test_no_removals_on_pure_addition(tmp_path) -> None:
    """Adding a function must not produce false removals."""
    repo = str(tmp_path)
    _setup_python_project(repo)

    (open(f"{repo}/calc.py", "a")).write("\n\ndef baz():\n    return 3\n")

    cfg = LocalTestConfig(repo_path=repo, base_branch="main")
    info = LocalDiffReader(cfg).read()
    removed = CodeAnalyzer(cfg).find_removed(info.files)

    assert removed == []


def test_generator_prunes_stale_tests_without_llm(tmp_path) -> None:
    """A removal-only change prunes the covering test with NO LLM call
    (llm=None would crash if any generation were attempted).
    """
    repo = str(tmp_path)
    _setup_python_project(repo)
    (open(f"{repo}/calc.py", "w")).write("def foo():\n    return 1\n")

    cfg = LocalTestConfig(repo_path=repo, base_branch="main")
    finder = TestFinder(cfg)
    generator = TestGenerator(cfg, finder, llm=None)

    removed = [RemovedFunction(file_path="calc.py", name="bar")]
    existing = finder.find(removed)
    assert existing, "test_calc.py should be discovered for calc.py"

    tests = generator.generate([], existing, removed=removed)

    assert len(tests) == 1
    pruned = tests[0]
    assert pruned.test_file_path == "tests/test_calc.py"
    assert "def test_bar" not in pruned.content
    assert "def test_foo" in pruned.content


def test_prune_keeps_unrelated_tests_intact(tmp_path) -> None:
    """Pruning must be surgical: only tests covering the removed
    function go; everything else survives byte-for-byte-ish.
    """
    repo = str(tmp_path)
    _setup_python_project(repo)

    cfg = LocalTestConfig(repo_path=repo, base_branch="main")
    generator = TestGenerator(cfg, TestFinder(cfg), llm=None)

    existing = ExistingTest(
        test_file_path="tests/test_calc.py",
        source_file_path="calc.py",
        content=open(f"{repo}/tests/test_calc.py").read(),
    )
    removed = [RemovedFunction(file_path="calc.py", name="bar")]

    from test_automator.languages import get_handler_for_file
    handler = get_handler_for_file("calc.py")
    pruned = generator._prune_removed(handler, existing, removed)

    assert "def test_bar" not in pruned.content
    assert "def test_foo" in pruned.content


def test_removed_source_file_prunes_all_its_tests(tmp_path) -> None:
    """Deleting the whole source file marks all its functions removed."""
    repo = str(tmp_path)
    _setup_python_project(repo)

    import os
    os.remove(f"{repo}/calc.py")

    cfg = LocalTestConfig(repo_path=repo, base_branch="main")
    info = LocalDiffReader(cfg).read()
    removed = CodeAnalyzer(cfg).find_removed(info.files)

    names = {r.name for r in removed}
    assert {"foo", "bar"} <= names
