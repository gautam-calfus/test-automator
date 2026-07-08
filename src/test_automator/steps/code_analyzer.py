"""Step 2: Identify functions/classes affected by the diff.

Thin dispatcher: looks up the language handler for each file and asks it
to extract affected functions. Language-specific AST parsing lives in
``languages.<name>.analyzer``.
"""

from __future__ import annotations

import os

from test_automator._logging import get_logger
from test_automator.config import LocalTestConfig
from test_automator.languages import get_handler_for_file
from test_automator.models import AffectedFunction, PRFile, RemovedFunction
from test_automator.utils.diff_parser import (
    extract_diff_hunk_for_range,
    parse_changed_lines,
)

logger = get_logger(__name__)

_ANALYZABLE_STATUSES = {"added", "modified"}


class CodeAnalyzer:
    """Per-file analysis dispatcher."""

    def __init__(self, config: LocalTestConfig) -> None:
        self._config = config

    def analyze(self, files: list[PRFile]) -> list[AffectedFunction]:
        affected: list[AffectedFunction] = []

        for pr_file in files:
            if pr_file.status not in _ANALYZABLE_STATUSES:
                continue
            functions = self._analyze_file(pr_file)
            affected.extend(functions)
            if functions:
                # v0.2.0: log NAMES, not just count. If the analyzer
                # somehow misses a function (e.g., grammar parse weirdness
                # on extension functions, infix functions, expression-bodied
                # functions), the user can spot which ones are missing by
                # comparing this list to their actual source diff.
                logger.info(
                    "analyzed file",
                    extra={
                        "file": pr_file.filename,
                        "functions": len(functions),
                        "function_names": [fn.name for fn in functions],
                    },
                )
            else:
                # Clear message when a file is in the diff but has no
                # testable changes. This typically means the changes are
                # to imports, class-level fields, constructor parameters,
                # or whitespace — none of which trigger method-level
                # test generation. Stage 4 will skip these files entirely
                # from prompt construction.
                logger.info(
                    "no method-body changes detected — file will be skipped",
                    extra={"file": pr_file.filename},
                )

        return affected

    def find_removed(self, files: list[PRFile]) -> list[RemovedFunction]:
        """Functions that existed at the merge-base but are gone now.

        v0.2: compares the function list extracted from each file's
        ``base_content`` (its merge-base version) against the current
        source. Anything present there but absent now was deleted (or
        renamed) — existing tests covering it can no longer compile, so
        the generator prunes them mechanically.

        A fully deleted source file counts all of its base functions as
        removed.
        """
        removed: list[RemovedFunction] = []

        for pr_file in files:
            if not pr_file.base_content:
                continue

            handler = get_handler_for_file(pr_file.filename)
            if handler is None:
                continue

            base_names = self._all_function_names(
                handler, pr_file.base_content, pr_file.filename
            )
            if not base_names:
                continue

            if pr_file.status == "removed":
                current_names: set[str] = set()
            else:
                current_source = self._read_source(pr_file.filename)
                if current_source is None:
                    continue
                current_names = self._all_function_names(
                    handler, current_source, pr_file.filename
                )

            gone = sorted(base_names - current_names)
            if gone:
                logger.info(
                    "removed functions detected — stale tests covering "
                    "them will be pruned",
                    extra={
                        "file": pr_file.filename,
                        "removed": gone,
                    },
                )
                removed.extend(
                    RemovedFunction(file_path=pr_file.filename, name=name)
                    for name in gone
                )

        return removed

    @staticmethod
    def _all_function_names(
        handler, source: str, filename: str
    ) -> set[str]:
        """Every function/method name in ``source``, using the handler's
        own extractor with an all-lines 'changed' set so nothing is
        filtered out.
        """
        all_lines = set(range(1, source.count("\n") + 2))
        try:
            functions = handler.extract_affected(source, filename, all_lines)
        except Exception:
            # Base-version parsing is best-effort: an unparsable old
            # revision should never block the pipeline.
            return set()
        return {fn.name for fn in functions}

    def _analyze_file(self, pr_file: PRFile) -> list[AffectedFunction]:
        handler = get_handler_for_file(pr_file.filename)
        if handler is None:
            logger.info(
                "no handler for file extension — skipping",
                extra={"file": pr_file.filename},
            )
            return []

        source = self._read_source(pr_file.filename)
        if source is None:
            return []

        changed_lines = (
            parse_changed_lines(pr_file.patch)
            if pr_file.patch
            else set(range(1, source.count("\n") + 2))
        )

        affected = handler.extract_affected(
            source, pr_file.filename, changed_lines
        )

        # Enrich each AffectedFunction with the specific diff hunk that
        # falls within its line range. The fresh/incremental prompts use
        # this to tell Claude what specifically changed, so generated
        # tests focus on the changes rather than re-testing the entire
        # function exhaustively.
        if pr_file.patch:
            for fn in affected:
                fn.diff_hunk = extract_diff_hunk_for_range(
                    pr_file.patch, fn.line_start, fn.line_end,
                )

        # Enrich each AffectedFunction with the file's class signatures.
        # This is the v0.2.0a6.post4 fix for the "Claude hallucinates
        # constructor parameters" problem. The handler may expose an
        # ``extract_class_signatures`` method (Kotlin, Java do); if so, we
        # use it. Python's handler currently doesn't, so this is a no-op
        # for Python files (class_context stays empty).
        #
        # v0.3.0a6 addition: some handlers (Java) can also resolve
        # project-internal imports and include their signatures. When
        # available, pass source_file_path and repo_root so the handler
        # can look up imports on disk.
        extract_signatures = getattr(handler, "extract_class_signatures", None)
        if extract_signatures is not None:
            try:
                import inspect
                sig = inspect.signature(extract_signatures)
                # Pass extra kwargs if the handler declares them
                kwargs: dict[str, str] = {}
                if "source_file_path" in sig.parameters:
                    kwargs["source_file_path"] = pr_file.filename
                if "repo_root" in sig.parameters:
                    kwargs["repo_root"] = self._config.repo_path
                class_context = extract_signatures(source, **kwargs)
            except Exception:
                # Defensive: never block the pipeline on signature
                # extraction. If parsing fails, leave context empty —
                # Claude will fall back to guessing from the function
                # body (the pre-post4 behavior).
                class_context = ""
            if class_context:
                for fn in affected:
                    fn.class_context = class_context

        return affected

    def _read_source(self, filename: str) -> str | None:
        full_path = os.path.join(self._config.repo_path, filename)
        if not os.path.isfile(full_path):
            logger.warning("file not found", extra={"path": full_path})
            return None
        with open(full_path, encoding="utf-8") as fh:
            return fh.read()
