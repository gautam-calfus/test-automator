"""Step 1: Read changed files from local git diff.

Filters files by extension based on what's registered in
``languages``. Stage 1 of the v0.2.0 refactor only registers Python, so
behavior matches the pre-refactor code; future stages will widen the
filter automatically when Java/Kotlin handlers register.
"""

from __future__ import annotations

import subprocess

from pr_test_automator_local._logging import get_logger
from pr_test_automator_local.config import LocalTestConfig
from pr_test_automator_local.languages import (
    all_source_extensions,
    get_handler_for_file,
)
from pr_test_automator_local.models import PRFile, PRInfo
from pr_test_automator_local.utils.exceptions import DiffReaderError

logger = get_logger(__name__)

_GIT_TIMEOUT = 30


class LocalDiffReader:
    """Reads changed source files from ``git diff`` against the base branch."""

    def __init__(self, config: LocalTestConfig) -> None:
        self._config = config

    def read(self) -> PRInfo:
        """Return changed source files since ``base_branch``."""
        self._verify_inside_repo()
        self._verify_base_branch_exists()
        self._warn_if_working_tree_dirty()

        head_branch = self._current_branch()
        author = self._current_user()

        files = self._collect_changed_files()
        source_files = [f for f in files if self._is_eligible_source(f.filename)]

        logger.info(
            "diff read",
            extra={
                "files_changed": len(source_files),
                "head": head_branch,
                "base": self._config.base_branch,
                "extensions": ",".join(all_source_extensions()) or "(none)",
            },
        )

        return PRInfo(
            number=0,
            title="local run",
            head_branch=head_branch,
            base_branch=self._config.base_branch,
            author=author,
            files=source_files,
        )

    def _verify_inside_repo(self) -> None:
        try:
            self._git("rev-parse", "--is-inside-work-tree", capture=True)
        except DiffReaderError as exc:
            raise DiffReaderError(
                f"Not inside a git repository at {self._config.repo_path}"
            ) from exc

    def _verify_base_branch_exists(self) -> None:
        try:
            self._git(
                "rev-parse",
                "--verify",
                self._config.base_branch,
                capture=True,
            )
        except DiffReaderError as exc:
            raise DiffReaderError(
                f"Base branch '{self._config.base_branch}' not found. "
                f"Try `git fetch origin {self._config.base_branch}:"
                f"{self._config.base_branch}` first, or use a different "
                f"--base-branch."
            ) from exc

    def _warn_if_working_tree_dirty(self) -> None:
        """Warn the user if there are uncommitted changes.

        The bot reads diffs from ``git diff BASE...HEAD`` — which only
        includes COMMITTED changes. Working-tree (uncommitted) and
        staged-but-not-committed changes are INVISIBLE to the bot.

        This caught a real user out: they made source modifications,
        didn't commit, then ran the bot expecting their changes to be
        tested. The bot ran cleanly against their previously-committed
        diff instead, leaving them confused why "their" functions
        weren't in the analyzed list. Hours of debugging.

        This warning makes the assumption explicit.
        """
        try:
            # `git diff --quiet` exits 0 if no working-tree changes,
            # 1 if there are. Same for --cached (staged changes).
            working_tree_dirty = self._git_exit_code(
                "diff", "--quiet"
            ) != 0
            index_dirty = self._git_exit_code(
                "diff", "--quiet", "--cached"
            ) != 0
        except Exception:
            # If we can't determine the state for any reason, skip the
            # warning rather than blocking the run.
            return

        if not (working_tree_dirty or index_dirty):
            return

        # Get the list of dirty files for a more useful message
        try:
            status = self._git("status", "--porcelain", capture=True)
            dirty_files = [
                line[3:] for line in status.splitlines() if line.strip()
            ]
        except Exception:
            dirty_files = []

        if dirty_files:
            files_msg = "\n  ".join(dirty_files[:10])
            if len(dirty_files) > 10:
                files_msg += f"\n  ... and {len(dirty_files) - 10} more"
            logger.warning(
                "uncommitted changes detected — these will NOT be "
                "tested. The bot only sees COMMITTED changes (git diff "
                "base...HEAD). Files with uncommitted changes:\n  %s\n"
                "Commit your changes before running, or be aware the "
                "current diff may not include what you intended to test.",
                files_msg,
            )
        else:
            logger.warning(
                "uncommitted changes detected — these will NOT be "
                "tested. The bot only sees COMMITTED changes."
            )

    def _git_exit_code(self, *args: str) -> int:
        """Run a git command and return its exit code. Doesn't raise on
        non-zero — used for ``--quiet`` commands where the exit code
        IS the answer.
        """
        proc = subprocess.run(
            ["git", *args],
            cwd=self._config.repo_path,
            capture_output=True,
            text=True,
        )
        return proc.returncode

    def _current_branch(self) -> str:
        return self._git(
            "rev-parse", "--abbrev-ref", "HEAD", capture=True
        ).strip()

    def _current_user(self) -> str:
        try:
            return self._git(
                "config", "user.name", capture=True
            ).strip() or "local-user"
        except DiffReaderError:
            return "local-user"

    def _collect_changed_files(self) -> list[PRFile]:
        """List filenames and statuses with git diff --name-status."""
        diff_range = f"{self._config.base_branch}...HEAD"
        raw = self._git(
            "diff", "--name-status", diff_range, capture=True
        )

        result: list[PRFile] = []
        status_map = {
            "A": "added",
            "M": "modified",
            "D": "removed",
            "R": "renamed",
        }
        for line in raw.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            status_char = parts[0][0]
            filename = parts[-1]
            status = status_map.get(status_char, "modified")

            patch = None if status == "removed" else self._get_patch(filename)

            result.append(
                PRFile(filename=filename, status=status, patch=patch)
            )

        return result

    def _get_patch(self, filename: str) -> str | None:
        diff_range = f"{self._config.base_branch}...HEAD"
        try:
            return self._git(
                "diff", diff_range, "--", filename, capture=True
            )
        except DiffReaderError:
            return None

    def _is_eligible_source(self, filename: str) -> bool:
        """Eligible if a registered language handler claims this extension,
        the file isn't a test file (per the handler's own definition), and
        it falls within ``source_root`` if set.
        """
        extensions = all_source_extensions()
        if not extensions or not filename.endswith(extensions):
            return False

        handler = get_handler_for_file(filename)
        if handler is None:
            return False

        if handler.is_test_file(filename):
            return False

        root = self._config.source_root
        if root and not filename.startswith(root.rstrip("/") + "/"):
            return False
        return True

    def _git(self, *args: str, capture: bool = False) -> str:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=self._config.repo_path,
                capture_output=capture,
                text=True,
                timeout=_GIT_TIMEOUT,
                check=True,
            )
            return proc.stdout if capture else ""
        except subprocess.CalledProcessError as exc:
            raise DiffReaderError(
                f"git {' '.join(args)} failed: {exc.stderr or exc}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DiffReaderError(
                f"git {' '.join(args)} timed out"
            ) from exc
