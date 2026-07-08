"""Tests for how the diff reader treats uncommitted changes.

v0.2 changed the default: the diff now compares the WORKING TREE
against the merge-base with the base branch, so uncommitted
modifications, staged changes, and untracked files are all part of the
analyzed change set. The old committed-only behavior (``git diff
BASE...HEAD``) is available via ``--committed-only``, and in that mode
the bot still warns when uncommitted changes would be invisible.

Original motivation (pre-v0.2): a developer modified source files but
didn't commit, then ran the bot expecting their changes to be tested.
The bot diffed committed changes only, so the modifications were
invisible — hours lost debugging. v0.2 makes those changes visible by
default instead of just warning about them.
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
    messages emitted during the test. The bot's root logger has
    ``propagate=False`` so pytest's caplog/capsys don't see them.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.INFO)
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
    (open(f"{repo}/calc.py", "w")).write("def foo():\n    return 1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True,
                   capture_output=True)
    # Create and switch to feature branch
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True,
                   capture_output=True)


def test_uncommitted_changes_are_included_by_default(
    tmp_path, captured_logs
) -> None:
    """v0.2: an uncommitted source modification shows up in the diff."""
    repo = str(tmp_path)
    _init_repo_with_base_branch(repo)

    # Modify a source file but do NOT commit
    (open(f"{repo}/calc.py", "w")).write("def foo():\n    return 99\n")

    config = LocalTestConfig(repo_path=repo, base_branch="main")
    info = LocalDiffReader(config).read()

    filenames = [f.filename for f in info.files]
    assert "calc.py" in filenames, (
        f"Expected uncommitted calc.py in the diff, got: {filenames}"
    )
    modified = next(f for f in info.files if f.filename == "calc.py")
    assert modified.status == "modified"
    assert modified.patch, "Expected a patch for the uncommitted change"
    # Base content is captured so removed-function detection can diff it
    assert modified.base_content is not None
    assert "return 1" in modified.base_content

    output = captured_logs.getvalue()
    assert "ARE included" in output, (
        f"Expected note that uncommitted changes are included, got:\n{output}"
    )


def test_untracked_files_are_included_as_added(tmp_path) -> None:
    """v0.2: brand-new untracked source files count as additions."""
    repo = str(tmp_path)
    _init_repo_with_base_branch(repo)

    (open(f"{repo}/brand_new.py", "w")).write("def baz():\n    return 3\n")

    config = LocalTestConfig(repo_path=repo, base_branch="main")
    info = LocalDiffReader(config).read()

    by_name = {f.filename: f for f in info.files}
    assert "brand_new.py" in by_name
    assert by_name["brand_new.py"].status == "added"
    # No patch → the analyzer treats every line as changed
    assert by_name["brand_new.py"].patch is None


def test_committed_only_mode_ignores_working_tree_and_warns(
    tmp_path, captured_logs
) -> None:
    """--committed-only restores the legacy behavior: uncommitted
    changes are invisible and the bot warns about them.
    """
    repo = str(tmp_path)
    _init_repo_with_base_branch(repo)

    (open(f"{repo}/calc.py", "w")).write("def foo():\n    return 99\n")

    config = LocalTestConfig(
        repo_path=repo, base_branch="main", committed_only=True
    )
    info = LocalDiffReader(config).read()

    assert info.files == [], (
        "committed-only mode must not see uncommitted changes"
    )
    output = captured_logs.getvalue()
    assert "uncommitted" in output.lower(), (
        f"Expected an uncommitted-changes warning, got:\n{output}"
    )
    assert "calc.py" in output


def test_committed_changes_visible_in_both_modes(tmp_path) -> None:
    """A committed change on the feature branch shows up regardless of
    mode.
    """
    repo = str(tmp_path)
    _init_repo_with_base_branch(repo)

    (open(f"{repo}/calc.py", "w")).write("def foo():\n    return 42\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "change foo"], cwd=repo,
                   check=True, capture_output=True)

    for committed_only in (False, True):
        config = LocalTestConfig(
            repo_path=repo, base_branch="main",
            committed_only=committed_only,
        )
        info = LocalDiffReader(config).read()
        filenames = [f.filename for f in info.files]
        assert "calc.py" in filenames, (
            f"committed_only={committed_only}: expected calc.py, "
            f"got {filenames}"
        )


def test_no_inclusion_note_when_working_tree_is_clean(
    tmp_path, captured_logs
) -> None:
    """If everything is committed, no uncommitted-changes chatter."""
    repo = str(tmp_path)
    _init_repo_with_base_branch(repo)

    (open(f"{repo}/feature.txt", "w")).write("feature work\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feature work"], cwd=repo,
                   check=True, capture_output=True)

    config = LocalTestConfig(repo_path=repo, base_branch="main")
    LocalDiffReader(config).read()

    output = captured_logs.getvalue()
    assert "uncommitted" not in output.lower(), (
        f"Expected NO uncommitted note on clean tree, got:\n{output}"
    )


def test_no_files_message_mentions_source_root_case() -> None:
    """When no eligible files are found, the message should remind the
    user about common causes including source_root case sensitivity
    and the diff mode — not say 'no Python source files' (which is
    misleading on Kotlin projects).
    """
    import inspect

    import test_automator.orchestrator as orch_module

    src = inspect.getsource(orch_module)

    # The misleading message must be gone
    assert "no Python source files changed" not in src, (
        "Misleading 'no Python source files' message still in orchestrator"
    )

    # The new message should mention common causes
    assert "no eligible source files changed" in src
    assert "case" in src.lower()  # mentions case sensitivity
    assert "committed" in src.lower()  # explains the diff mode
