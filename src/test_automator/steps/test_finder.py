"""Step 3: Locate existing test files.

Thin dispatcher: uses the language handler to compute candidate paths,
then probes each for existence on disk.
"""

from __future__ import annotations

import os

from test_automator._logging import get_logger
from test_automator.config import LocalTestConfig
from test_automator.languages import get_handler_for_file
from test_automator.models import AffectedFunction, ExistingTest

logger = get_logger(__name__)


class TestFinder:
    """Looks up test files using each language's conventions."""

    def __init__(self, config: LocalTestConfig) -> None:
        self._config = config

    def find(self, affected: list[AffectedFunction]) -> list[ExistingTest]:
        source_files = {fn.file_path for fn in affected}
        results: list[ExistingTest] = []

        for source_path in source_files:
            test_file = self._find_test_file(source_path)
            if test_file:
                results.append(test_file)
            else:
                logger.info(
                    "no existing tests", extra={"source": source_path}
                )

        return results

    def _find_test_file(self, source_path: str) -> ExistingTest | None:
        handler = get_handler_for_file(source_path)
        if handler is None:
            return None

        # Apply runtime config to the handler (Python's test_dirs override).
        # Other handlers can also expose a `configure` if they need it.
        configure = getattr(handler, "configure", None)
        if callable(configure):
            configure(self._config.all_test_dirs)

        # Phase 1: check the conventional candidate paths
        for candidate in handler.candidate_test_paths(source_path):
            full = os.path.join(self._config.repo_path, candidate)
            if os.path.isfile(full):
                logger.info(
                    "found existing tests", extra={"path": candidate}
                )
                return ExistingTest(
                    test_file_path=candidate,
                    source_file_path=source_path,
                    content=self._read(full),
                )

        # Phase 2: filename-based fallback search.
        # If the handler exposes ``find_existing_test_file_by_search``,
        # use it to look for tests at non-conventional paths. This is the
        # v0.2.0+ safety net for the case where a test file exists at a
        # slightly different location than the convention suggests —
        # without this, the bot would silently create a duplicate at the
        # conventional path. See Kotlin handler for full rationale.
        search_fallback = getattr(
            handler, "find_existing_test_file_by_search", None
        )
        if callable(search_fallback):
            found = search_fallback(self._config.repo_path, source_path)
            if found:
                full = os.path.join(self._config.repo_path, found)
                if os.path.isfile(full):
                    logger.info(
                        "found existing tests (via fallback search)",
                        extra={"path": found},
                    )
                    return ExistingTest(
                        test_file_path=found,
                        source_file_path=source_path,
                        content=self._read(full),
                    )

        return None

    @staticmethod
    def _read(path: str) -> str:
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def suggest_test_path(
        self,
        source_path: str,
        existing: ExistingTest | None = None,
    ) -> str:
        """Where to write the generated test for ``source_path``.

        If an existing test file was found, reuse its path. Otherwise, ask
        the language handler for the canonical fresh-file location.
        """
        if existing:
            return existing.test_file_path

        handler = get_handler_for_file(source_path)
        if handler is None:
            # Should not happen for any file we'd be generating tests for,
            # but fall back to the legacy Python convention just in case.
            stem = os.path.splitext(os.path.basename(source_path))[0]
            preferred_dir = (
                self._config.test_dirs[0]
                if self._config.test_dirs
                else "tests"
            )
            return os.path.join(preferred_dir, f"test_{stem}.py")

        configure = getattr(handler, "configure", None)
        if callable(configure):
            configure(self._config.all_test_dirs)
        return handler.suggest_test_path(source_path)
