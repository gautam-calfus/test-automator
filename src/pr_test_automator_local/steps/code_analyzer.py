"""Step 2: Identify functions/classes affected by the diff.

Thin dispatcher: looks up the language handler for each file and asks it
to extract affected functions. Language-specific AST parsing lives in
``languages.<name>.analyzer``.
"""

from __future__ import annotations

import os

from pr_test_automator_local._logging import get_logger
from pr_test_automator_local.config import LocalTestConfig
from pr_test_automator_local.languages import get_handler_for_file
from pr_test_automator_local.models import AffectedFunction, PRFile
from pr_test_automator_local.utils.diff_parser import (
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
                logger.info(
                    "analyzed file",
                    extra={
                        "file": pr_file.filename,
                        "functions": len(functions),
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
        # ``extract_class_signatures`` method (Kotlin does); if so, we
        # use it. Python's handler currently doesn't, so this is a no-op
        # for Python files (class_context stays empty).
        extract_signatures = getattr(handler, "extract_class_signatures", None)
        if extract_signatures is not None:
            try:
                class_context = extract_signatures(source)
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
