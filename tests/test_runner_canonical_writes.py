"""Tests for the v0.2 test-runner write strategy.

Pre-v0.2 the runner wrote generated tests as renamed temp copies
(``_PRBotXTest``) ALONGSIDE the canonical test file. When the canonical
file itself was broken by source changes (e.g. it referenced a deleted
method), compilation failed on every run no matter how correct the
regenerated tests were — the fix loop could never converge.

v0.2 writes the generated content AT the canonical path, backing up the
original and restoring it after the run (deleting files that didn't
exist before). The run therefore validates exactly what the committer
will write, and the working tree is untouched afterwards.
"""

from __future__ import annotations

import os

from test_automator.config import LocalTestConfig
from test_automator.models import GeneratedTest
from test_automator.steps.test_runner import TestRunner


def _make_runner(tmp_path) -> TestRunner:
    cfg = LocalTestConfig(repo_path=str(tmp_path), base_branch="main")
    return TestRunner(cfg)


def test_write_replaces_canonical_and_backs_up_original(tmp_path) -> None:
    runner = _make_runner(tmp_path)
    os.makedirs(f"{tmp_path}/tests")
    original = "BROKEN ORIGINAL CONTENT\n"
    (open(f"{tmp_path}/tests/test_calc.py", "w")).write(original)

    gen = GeneratedTest(
        source_file_path="calc.py",
        test_file_path="tests/test_calc.py",
        content="def test_foo():\n    assert True\n",
        covered_functions=["foo"],
    )

    written, backups = runner._write_tests([gen])

    dest = f"{tmp_path}/tests/test_calc.py"
    assert written == [dest]
    # Canonical file now holds the GENERATED content (not a temp copy)
    assert open(dest).read() == gen.content
    assert not os.path.exists(f"{tmp_path}/tests/_PRBottest_calc.py")
    # Original preserved for restore
    assert backups == {dest: original}


def test_cleanup_restores_preexisting_file(tmp_path) -> None:
    runner = _make_runner(tmp_path)
    os.makedirs(f"{tmp_path}/tests")
    original = "ORIGINAL\n"
    dest = f"{tmp_path}/tests/test_calc.py"
    (open(dest, "w")).write(original)

    gen = GeneratedTest(
        source_file_path="calc.py",
        test_file_path="tests/test_calc.py",
        content="GENERATED\n",
        covered_functions=[],
    )
    _, backups = runner._write_tests([gen])
    runner._cleanup(backups)

    assert open(dest).read() == original


def test_cleanup_removes_fresh_file(tmp_path) -> None:
    """A test file that did NOT exist before the run is deleted on
    cleanup — the committer step owns the final write.
    """
    runner = _make_runner(tmp_path)

    gen = GeneratedTest(
        source_file_path="calc.py",
        test_file_path="tests/test_new.py",
        content="def test_x():\n    assert True\n",
        covered_functions=["x"],
    )
    written, backups = runner._write_tests([gen])
    dest = f"{tmp_path}/tests/test_new.py"
    assert written == [dest]
    assert backups == {dest: None}

    runner._cleanup(backups)
    assert not os.path.exists(dest)


def test_first_backup_wins_on_duplicate_paths(tmp_path) -> None:
    """Two generated tests targeting the same path must not clobber the
    ORIGINAL backup with intermediate generated content.
    """
    runner = _make_runner(tmp_path)
    os.makedirs(f"{tmp_path}/tests")
    original = "ORIGINAL\n"
    dest = f"{tmp_path}/tests/test_calc.py"
    (open(dest, "w")).write(original)

    gens = [
        GeneratedTest(
            source_file_path="calc.py",
            test_file_path="tests/test_calc.py",
            content="FIRST\n",
            covered_functions=[],
        ),
        GeneratedTest(
            source_file_path="calc.py",
            test_file_path="tests/test_calc.py",
            content="SECOND\n",
            covered_functions=[],
        ),
    ]
    _, backups = runner._write_tests(gens)

    # Last write is on disk, but the backup is the true original
    assert open(dest).read() == "SECOND\n"
    assert backups == {dest: original}

    runner._cleanup(backups)
    assert open(dest).read() == original


def test_end_to_end_broken_existing_file_gets_validated_fix(tmp_path) -> None:
    """The scenario v0.2 exists for: the on-disk test file is broken
    (references a removed function); the generated replacement passes;
    the original is restored after the run.
    """
    import shutil
    import subprocess

    if shutil.which("git") is None:
        return

    repo = str(tmp_path)
    (open(f"{repo}/calc.py", "w")).write("def foo():\n    return 1\n")
    os.makedirs(f"{repo}/tests")
    (open(f"{repo}/tests/test_calc.py", "w")).write(
        "from calc import foo, bar  # bar no longer exists!\n"
    )

    cfg = LocalTestConfig(repo_path=repo, base_branch="main")
    runner = TestRunner(cfg)

    gen = GeneratedTest(
        source_file_path="calc.py",
        test_file_path="tests/test_calc.py",
        content="from calc import foo\n\n\ndef test_foo():\n    assert foo() == 1\n",
        covered_functions=["foo"],
    )
    result = runner.run([gen])

    # The broken original was NOT part of the run: the generated content
    # replaced it, so the run is green.
    assert result.is_passing, result.output
    assert result.passed == 1
    # ...and the working tree is back to its (broken) original state;
    # the committer decides what finally lands on disk.
    assert "bar no longer exists" in open(f"{repo}/tests/test_calc.py").read()
