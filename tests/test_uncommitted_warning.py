"""Tests for the v0.2.0 uncommitted-changes warning.

Real user scenario: a developer modifies source files but doesn't commit,
then runs the bot expecting their changes to be tested. The bot diffs
``git diff BASE...HEAD`` which only sees COMMITTED changes — so the
user's uncommitted modifications are invisible. They lose hours debugging
"why didn't the bot test the functions I changed?"

This test verifies the bot now warns about uncommitted changes at
pipeline start so the user catches the issue immediately.
"""

from __future__ import annotations

import io
import logging
import subprocess

import pytest

from test_automator.config import LocalTestConfig
from test_automator.steps.local_diff_reader import LocalDiffReader


@pytest.fixture
def captured_logs():
    """Attach a StringIO handler to the bot's logger so we can read
    warnings emitted during the test. The bot's root logger has
    ``propagate=False`` so pytest's caplog/capsys don't see them.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.WARNING)
    bot_logger = logging.getLogger("test_automator")
    bot_logger.addHandler(handler)
    yield stream
    bot_logger.removeHandler(handler)


def _init_repo_with_base_branch(repo: str) -> None:
    """Set up a minimal git repo with a base branch and one commit on
    a feature branch, so the diff reader has something to work with.
    """
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo,
                   check=True, capture_output=True)
    # Initial commit on main
    (open(f"{repo}/README.md", "w")).write("# test\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True,
                   capture_output=True)
    # Create and switch to feature branch
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True,
                   capture_output=True)


def test_warns_when_working_tree_has_uncommitted_changes(
    tmp_path, captured_logs
) -> None:
    """If a file is modified but not committed, the bot warns about it
    at the start of the run.
    """
    repo = str(tmp_path)
    _init_repo_with_base_branch(repo)

    # Modify README (uncommitted change in working tree)
    (open(f"{repo}/README.md", "w")).write("# test\nmodified\n")

    config = LocalTestConfig(repo_path=repo, base_branch="main")
    reader = LocalDiffReader(config)
    reader.read()

    output = captured_logs.getvalue()
    assert "uncommitted" in output.lower(), (
        f"Expected an uncommitted-changes warning, got:\n{output}"
    )
    assert "README.md" in output


def test_warns_when_staged_changes_present(tmp_path, captured_logs) -> None:
    """Staged-but-not-committed changes should also trigger the warning."""
    repo = str(tmp_path)
    _init_repo_with_base_branch(repo)

    # Stage a new file but don't commit
    (open(f"{repo}/staged.txt", "w")).write("staged but not committed\n")
    subprocess.run(["git", "add", "staged.txt"], cwd=repo, check=True,
                   capture_output=True)

    config = LocalTestConfig(repo_path=repo, base_branch="main")
    reader = LocalDiffReader(config)
    reader.read()

    output = captured_logs.getvalue()
    assert "uncommitted" in output.lower(), (
        f"Expected warning for staged changes, got:\n{output}"
    )


def test_no_warning_when_working_tree_is_clean(tmp_path, captured_logs) -> None:
    """If everything is committed, no warning."""
    repo = str(tmp_path)
    _init_repo_with_base_branch(repo)

    # Make a commit on the feature branch
    (open(f"{repo}/feature.txt", "w")).write("feature work\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feature work"], cwd=repo,
                   check=True, capture_output=True)

    config = LocalTestConfig(repo_path=repo, base_branch="main")
    reader = LocalDiffReader(config)
    reader.read()

    output = captured_logs.getvalue()
    assert "uncommitted" not in output.lower(), (
        f"Expected NO uncommitted warning on clean tree, got:\n{output}"
    )


def test_no_files_message_mentions_source_root_case() -> None:
    """When no eligible files are found, the message should remind the
    user about common causes including source_root case sensitivity
    and uncommitted changes — not say 'no Python source files' (which
    is misleading on Kotlin projects).
    """
    import test_automator.orchestrator as orch_module
    import inspect

    src = inspect.getsource(orch_module)

    # The misleading message must be gone
    assert "no Python source files changed" not in src, (
        "Misleading 'no Python source files' message still in orchestrator"
    )

    # The new message should mention common causes
    assert "no eligible source files changed" in src
    assert "case" in src.lower()  # mentions case sensitivity
    assert "COMMITTED" in src or "committed" in src.lower()

