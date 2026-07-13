"""Step 1: Read changed files from local git diff.

v0.2: the diff now compares the WORKING TREE against the merge-base
with the base branch — not ``base...HEAD``. That means uncommitted
modifications, staged changes, and untracked new files are all part of
the analyzed change set, matching what the developer actually sees in
their editor. Pass ``--committed-only`` to restore the old
committed-changes-only behavior.

Filters files by extension based on what's registered in ``languages``.
"""

from __future__ import annotations

import subprocess

from test_automator._logging import get_logger
from test_automator.config import LocalTestConfig
from test_automator.languages import (
    all_source_extensions,
    get_handler_for_file,
)
from test_automator.models import PRFile, PRInfo
from test_automator.utils.exceptions import DiffReaderError

logger = get_logger(__name__)

_GIT_TIMEOUT = 30


class LocalDiffReader:
    """Reads changed source files from ``git diff`` against the base branch."""

    def __init__(self, config: LocalTestConfig) -> None:
        self._config = config

    def read(self) -> PRInfo:
        """Return changed source files since ``base_branch``."""
        self._verify_inside_repo()
        self._maybe_fetch_base()
        self._verify_base_branch_exists()
        if self._committed_only():
            self._warn_if_working_tree_dirty()
        else:
            self._note_uncommitted_changes_included()

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

    def _committed_only(self) -> bool:
        return bool(getattr(self._config, "committed_only", False))

    def _maybe_fetch_base(self) -> None:
        """When the base branch is a remote-tracking ref (``origin/x``),
        fetch it first so the diff reflects the LIVE remote, not a
        stale local cache. ``origin/develop`` alone is only as fresh as
        your last ``git fetch`` — this makes ``--base-branch
        origin/develop`` behave the way people expect (check the remote).

        Only fires for a ``<remote>/<branch>`` form where ``<remote>``
        is a real git remote. Best-effort: a fetch failure (offline,
        auth) logs a warning and we proceed with the cached ref.
        Skipped when ``--no-fetch`` is set.
        """
        if not getattr(self._config, "fetch_base", True):
            return
        base = self._config.base_branch
        if "/" not in base:
            return  # a plain local branch — nothing to fetch
        remote, _, branch = base.partition("/")
        if not branch:
            return
        try:
            remotes = self._git("remote", capture=True).split()
        except DiffReaderError:
            return
        if remote not in remotes:
            return  # not a remote ref (e.g. a local branch with a slash)
        logger.info(
            "fetching %s from remote %s so the diff reflects the live "
            "remote…",
            branch, remote,
        )
        try:
            self._git("fetch", remote, branch)
        except DiffReaderError as exc:
            logger.warning(
                "could not fetch %s/%s (%s) — proceeding with the cached "
                "remote-tracking ref; pass --no-fetch to silence, or run "
                "`git fetch %s %s` yourself.",
                remote, branch, str(exc).splitlines()[0] if str(exc) else "",
                remote, branch,
            )

    def _note_uncommitted_changes_included(self) -> None:
        """Tell the user their uncommitted changes ARE being analyzed.

        v0.2 changed the diff source from ``base...HEAD`` to
        merge-base-vs-working-tree, so uncommitted and untracked
        changes are included. This log line makes the new behavior
        explicit for users accustomed to the old committed-only diff.
        """
        try:
            dirty = self._git_exit_code("diff", "--quiet") != 0
            staged = self._git_exit_code("diff", "--quiet", "--cached") != 0
        except Exception:
            return
        if dirty or staged:
            logger.info(
                "working tree has uncommitted changes — these ARE "
                "included in the diff. Pass --committed-only to diff "
                "committed changes only."
            )

    def _warn_if_working_tree_dirty(self) -> None:
        """Warn the user if there are uncommitted changes.

        Only relevant with ``--committed-only``: in that mode the bot
        reads diffs from ``git diff BASE...HEAD`` — which only includes
        COMMITTED changes. Working-tree (uncommitted) and
        staged-but-not-committed changes are INVISIBLE to the bot.

        This caught a real user out: they made source modifications,
        didn't commit, then ran the bot expecting their changes to be
        tested. The bot ran cleanly against their previously-committed
        diff instead, leaving them confused why "their" functions
        weren't in the analyzed list. Hours of debugging. (That incident
        is why working-tree diffing is now the default.)

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

    def _merge_base(self) -> str:
        """SHA of the merge-base between the base branch and HEAD —
        the same starting point ``base...HEAD`` used, but usable as a
        single commit to diff the working tree against.
        """
        return self._git(
            "merge-base", self._config.base_branch, "HEAD", capture=True
        ).strip()

    def _collect_changed_files(self) -> list[PRFile]:
        """List changed files with their statuses and patches.

        Default (v0.2): diff the WORKING TREE against the merge-base
        with the base branch, so uncommitted and staged changes are
        included, plus untracked files as additions.

        ``--committed-only``: legacy ``git diff base...HEAD`` behavior.
        """
        if self._committed_only():
            diff_base = f"{self._config.base_branch}...HEAD"
            include_untracked = False
        else:
            diff_base = self._merge_base()
            include_untracked = True

        raw = self._git(
            "diff", "--name-status", diff_base, capture=True
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

            # Patches and base content cost a git subprocess each —
            # only fetch them for files the pipeline will analyze.
            eligible = self._is_eligible_source(filename)
            patch = (
                None if (status == "removed" or not eligible)
                else self._get_patch(diff_base, filename)
            )
            # Base version's content, used by the analyzer to detect
            # REMOVED functions. Only meaningful for files that existed
            # at the base (modified/removed/renamed).
            base_content = (
                None if (status == "added" or not eligible)
                else self._get_base_content(diff_base, filename)
            )

            result.append(
                PRFile(
                    filename=filename,
                    status=status,
                    patch=patch,
                    base_content=base_content,
                )
            )

        if include_untracked:
            result.extend(self._collect_untracked_files())

        return result

    def _collect_untracked_files(self) -> list[PRFile]:
        """Untracked (never-committed) files count as additions.

        ``patch=None`` makes the analyzer treat every line as changed,
        which is exactly right for a brand-new file.
        """
        raw = self._git(
            "ls-files", "--others", "--exclude-standard", capture=True
        )
        return [
            PRFile(filename=line.strip(), status="added", patch=None)
            for line in raw.splitlines()
            if line.strip()
        ]

    def _get_patch(self, diff_base: str, filename: str) -> str | None:
        try:
            return self._git(
                "diff", diff_base, "--", filename, capture=True
            )
        except DiffReaderError:
            return None

    def _get_base_content(self, diff_base: str, filename: str) -> str | None:
        """File content at the base of the diff (merge-base commit).

        For the legacy three-dot range, resolve the merge-base first so
        ``git show`` gets a real commit.
        """
        ref = diff_base
        if "..." in ref:
            try:
                ref = self._merge_base()
            except DiffReaderError:
                return None
        try:
            return self._git("show", f"{ref}:{filename}", capture=True)
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
