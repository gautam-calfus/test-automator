"""Step 4: Generate tests using the LLM bridge (Claude Code by default).

Thin dispatcher: groups affected functions by file, looks up each file's
language handler, and delegates prompt construction and merging to it.
"""

from __future__ import annotations

from pr_test_automator_local._logging import get_logger
from pr_test_automator_local.config import LocalTestConfig
from pr_test_automator_local.languages import get_handler_for_file
from pr_test_automator_local.languages.base import LanguageHandler
from pr_test_automator_local.llm_bridge import LLMBridge
from pr_test_automator_local.models import (
    AffectedFunction,
    ExistingTest,
    GeneratedTest,
)
from pr_test_automator_local.steps.test_finder import TestFinder
from pr_test_automator_local.utils.diff_parser import extract_code_block
from pr_test_automator_local.utils.exceptions import TestGeneratorError

logger = get_logger(__name__)


def _extract_code(
    handler: LanguageHandler, raw: str, mode: str, source_path: str
) -> str:
    """Dispatch to the handler's language-specific extractor if present.

    The ``extract_code`` hook is not part of the LanguageHandler protocol
    (yet) — it's an optional method. Handlers without it fall back to
    the legacy ``extract_code_block`` (Python-style markdown parsing).

    ``mode`` is either ``"fresh"`` or ``"incremental"``. Different
    extractors handle them differently: fresh expects a complete file;
    incremental expects just @Test method declarations.

    Raises TestGeneratorError if extraction fails — i.e., the LLM
    response contained no recognizable target-language code. This
    happens when Claude returned pure prose or refused the request.
    """
    extract_hook = getattr(handler, "extract_code", None)
    if callable(extract_hook):
        try:
            return extract_hook(raw, mode=mode)
        except Exception as exc:
            raise TestGeneratorError(
                f"Could not extract {handler.name} code from LLM response "
                f"for {source_path} (mode={mode}): {exc}"
            ) from exc
    # Fall back to legacy Python-style extraction for languages without
    # a custom hook
    return extract_code_block(raw)


class TestGenerator:
    """Orchestrates test generation across one or more languages."""

    def __init__(
        self,
        config: LocalTestConfig,
        test_finder: TestFinder,
        llm: LLMBridge,
    ) -> None:
        self._config = config
        self._test_finder = test_finder
        self._llm = llm

    def generate(
        self,
        affected: list[AffectedFunction],
        existing_tests: list[ExistingTest],
    ) -> list[GeneratedTest]:
        by_file = self._group_by_file(affected)
        existing_by_source = {t.source_file_path: t for t in existing_tests}
        results: list[GeneratedTest] = []
        failed_files: list[tuple[str, str]] = []

        for source_path, functions in by_file.items():
            handler = get_handler_for_file(source_path)
            if handler is None:
                logger.warning(
                    "no language handler — skipping",
                    extra={"file": source_path},
                )
                continue

            # Some handlers (Python's) need to know the configured test_dirs.
            configure = getattr(handler, "configure", None)
            if callable(configure):
                configure(self._config.all_test_dirs)

            existing = existing_by_source.get(source_path)

            # v0.3.0: per-file LLM failures are non-fatal. If Claude Code
            # hits its session quota mid-batch (or any other transient
            # error occurs), we log the file that failed and continue
            # with the remaining files. Otherwise a single failure late
            # in a large batch loses ALL work — up to 40+ minutes of
            # quota-consuming generation calls.
            try:
                if existing:
                    generated = self._generate_incremental(
                        handler, source_path, functions, existing
                    )
                    mode = "incremental"
                else:
                    generated = self._generate_fresh(
                        handler, source_path, functions
                    )
                    mode = "fresh"
            except TestGeneratorError as exc:
                logger.warning(
                    "test generation FAILED for %s — continuing with "
                    "remaining files. Error: %s",
                    source_path, exc,
                )
                failed_files.append((source_path, str(exc)))
                continue

            results.append(generated)
            logger.info(
                "generated tests",
                extra={"source": source_path, "mode": mode},
            )

        if failed_files:
            logger.warning(
                "test generation completed with %d failure(s) out of %d "
                "files. Successful: %d. Failed files: %s",
                len(failed_files),
                len(by_file),
                len(results),
                ", ".join(path for path, _ in failed_files),
            )

        # Only raise if EVERYTHING failed (no results at all). Partial
        # success is worth preserving — the successful files will be
        # written to disk by the test_committer.
        if not results and failed_files:
            first_error = failed_files[0][1]
            raise TestGeneratorError(
                f"All {len(failed_files)} file(s) failed test generation. "
                f"First error: {first_error}"
            )

        return results

    def _generate_fresh(
        self,
        handler: LanguageHandler,
        source_path: str,
        functions: list[AffectedFunction],
    ) -> GeneratedTest:
        try:
            user_prompt = handler.user_prompt_fresh(source_path, functions)
            system_prompt = handler.system_prompt_fresh()
        except NotImplementedError as exc:
            raise TestGeneratorError(
                f"Test generation for '{handler.name}' is not implemented "
                f"in this release. Affected file: {source_path}. {exc}"
            ) from exc

        try:
            raw = self._llm.generate(system_prompt, user_prompt)
        except Exception as exc:
            raise TestGeneratorError(
                f"LLM failed for {source_path}: {exc}"
            ) from exc

        code = _extract_code(handler, raw, mode="fresh", source_path=source_path)
        test_path = self._test_finder.suggest_test_path(
            source_path, existing=None
        )

        return GeneratedTest(
            source_file_path=source_path,
            test_file_path=test_path,
            content=code,
            covered_functions=[fn.qualified_name for fn in functions],
        )

    def _generate_incremental(
        self,
        handler: LanguageHandler,
        source_path: str,
        functions: list[AffectedFunction],
        existing: ExistingTest,
    ) -> GeneratedTest:
        try:
            existing_tests = handler.parse_existing_tests(existing.content)
        except NotImplementedError as exc:
            raise TestGeneratorError(
                f"Incremental merge for '{handler.name}' is not implemented "
                f"in this release. Delete the existing test file at "
                f"{existing.test_file_path} to fall back to fresh "
                f"generation, or wait for a release that supports it. {exc}"
            ) from exc

        # Identify which existing tests cover the modified functions, so
        # they can be removed and replaced.
        tests_to_remove = []
        for fn in functions:
            for t in existing_tests:
                if handler.covers(t.name, fn.name):
                    tests_to_remove.append(t)

        # These helpers aren't on the LanguageHandler protocol yet — they
        # are Python-specific for now. The hasattr check makes the codepath
        # safe even if a future handler doesn't implement them.
        extract_test_source = getattr(handler, "extract_test_source", None)
        remove_tests = getattr(handler, "remove_tests", None)
        if extract_test_source is None or remove_tests is None:
            raise TestGeneratorError(
                f"Language '{handler.name}' does not support incremental "
                f"merge yet. Delete the existing test file or use fresh "
                f"generation."
            )

        removed_tests_code = extract_test_source(
            existing.content, tests_to_remove
        )
        trimmed_existing = remove_tests(existing.content, tests_to_remove)

        try:
            user_prompt = handler.user_prompt_incremental(
                source_path,
                existing,
                functions,
                trimmed_existing,
                removed_tests_code,
            )
            system_prompt = handler.system_prompt_incremental()
        except NotImplementedError as exc:
            raise TestGeneratorError(
                f"Incremental prompts for '{handler.name}' not implemented "
                f"in this release. {exc}"
            ) from exc

        try:
            raw = self._llm.generate(system_prompt, user_prompt)
        except Exception as exc:
            raise TestGeneratorError(
                f"LLM failed for {source_path}: {exc}"
            ) from exc

        new_test_code = _extract_code(
            handler, raw, mode="incremental", source_path=source_path
        ).strip()
        merged = handler.merge_new_tests(trimmed_existing, new_test_code)

        return GeneratedTest(
            source_file_path=source_path,
            test_file_path=existing.test_file_path,
            content=merged,
            covered_functions=[fn.qualified_name for fn in functions],
        )

    @staticmethod
    def _group_by_file(
        affected: list[AffectedFunction],
    ) -> dict[str, list[AffectedFunction]]:
        groups: dict[str, list[AffectedFunction]] = {}
        for fn in affected:
            groups.setdefault(fn.file_path, []).append(fn)
        return groups
