"""Step 4: Generate tests using the LLM bridge (Claude Code by default).

Thin dispatcher: groups affected functions by file, looks up each file's
language handler, and delegates prompt construction and merging to it.
"""

from __future__ import annotations

from test_automator._logging import get_logger
from test_automator.config import LocalTestConfig
from test_automator.languages import get_handler_for_file
from test_automator.languages.base import LanguageHandler
from test_automator.llm_bridge import LLMBridge
from test_automator.models import (
    AffectedFunction,
    ExistingTest,
    GeneratedTest,
    RemovedFunction,
)
from test_automator.steps.example_finder import (
    ExampleFinder,
    format_example_block,
)
from test_automator.steps.test_finder import TestFinder
from test_automator.utils import gen_cache
from test_automator.utils.diff_parser import extract_code_block
from test_automator.utils.exceptions import (
    LLMSessionLimitError,
    TestGeneratorError,
)

logger = get_logger(__name__)


# Maximum changed functions per LLM call. When a file's diff touches
# more functions than this, generation is split into multiple calls:
# the first produces the test file, each later batch adds @Test methods
# to it (incremental mode against the file generated so far).
#
# Real failure this prevents: Acme's QuestionRoutingService had 7
# changed methods in one diff. A single prompt asked Claude for tests
# covering all 7 and the response blew the CLI's output-token cap —
# three runs died on "response exceeded the 32000 output token
# maximum". Four functions per call keeps responses comfortably under
# the cap while still amortizing prompt overhead.
MAX_FUNCTIONS_PER_CALL = 4


def _chunk(
    items: list[AffectedFunction], size: int
) -> list[list[AffectedFunction]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


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
        self._example_finder = ExampleFinder(config)

    def generate(
        self,
        affected: list[AffectedFunction],
        existing_tests: list[ExistingTest],
        removed: list[RemovedFunction] | None = None,
    ) -> list[GeneratedTest]:
        return list(self.iter_generate(affected, existing_tests, removed))

    def iter_generate(
        self,
        affected: list[AffectedFunction],
        existing_tests: list[ExistingTest],
        removed: list[RemovedFunction] | None = None,
    ):
        """Yield GeneratedTest objects one at a time, as each file's
        generation completes.

        Streaming (vs returning the full batch) lets the orchestrator
        run and fix each file's tests IMMEDIATELY, while the next
        file's generation hasn't happened yet — feedback lands per
        file instead of after the whole batch, and a failure is
        attributed to its file by construction.
        """
        by_file = self._group_by_file(affected)
        existing_by_source = {t.source_file_path: t for t in existing_tests}
        removed_by_file = self._group_removed_by_file(removed or [])
        produced = 0
        failed_files: list[tuple[str, str]] = []

        for source_path, functions in by_file.items():
            handler = get_handler_for_file(source_path)
            if handler is None:
                logger.warning(
                    "no language handler — skipping",
                    extra={"file": source_path},
                )
                continue

            functions = self._cap_functions(source_path, functions)

            logger.info(
                "→ generating tests | file=%s | functions(%d)=%s",
                source_path,
                len(functions),
                ", ".join(fn.name for fn in functions),
            )

            # Some handlers (Python's) need to know the configured test_dirs.
            configure = getattr(handler, "configure", None)
            if callable(configure):
                configure(self._config.all_test_dirs)

            existing = existing_by_source.get(source_path)

            # v0.2: before generating, prune tests that cover functions
            # REMOVED from this source file. They reference symbols that
            # no longer exist, so leaving them in guarantees a compile /
            # import failure regardless of how good the new tests are.
            removed_here = removed_by_file.get(source_path, [])
            if existing and removed_here:
                existing = self._prune_removed(
                    handler, existing, removed_here
                )

            # Determinism + token savings: if nothing that shapes the
            # output changed since a previous run, reuse the cached
            # test file verbatim and skip the LLM entirely. Same input
            # -> same output, which is what makes repeated runs
            # trustworthy for devs.
            cache_on = getattr(self._config, "use_cache", True)
            cache_key = None
            if cache_on:
                cache_key = gen_cache.compute_key(
                    source_path=source_path,
                    function_sources=[fn.source_code for fn in functions],
                    mode=("incremental" if existing else "fresh"),
                    existing_content=existing.content if existing else "",
                )
                cached = gen_cache.get(self._config.repo_path, cache_key)
                if cached is not None:
                    test_path = (
                        existing.test_file_path
                        if existing
                        else self._test_finder.suggest_test_path(
                            source_path, existing=None
                        )
                    )
                    logger.info(
                        "    ⟳ cache hit — reusing tests, no LLM call | "
                        "file=%s",
                        source_path,
                    )
                    produced += 1
                    yield GeneratedTest(
                        source_file_path=source_path,
                        test_file_path=test_path,
                        content=cached,
                        covered_functions=[
                            fn.qualified_name for fn in functions
                        ],
                    )
                    continue

            # v0.3.0: per-file LLM failures are non-fatal. If Claude Code
            # hits its session quota mid-batch (or any other transient
            # error occurs), we log the file that failed and continue
            # with the remaining files. Otherwise a single failure late
            # in a large batch loses ALL work — up to 40+ minutes of
            # quota-consuming generation calls.
            try:
                generated, mode = self._generate_batched(
                    handler, source_path, functions, existing
                )
            except TestGeneratorError as exc:
                logger.warning(
                    "test generation FAILED for %s — continuing with "
                    "remaining files. Error: %s",
                    source_path, exc,
                )
                failed_files.append((source_path, str(exc)))
                continue

            if cache_on and cache_key:
                gen_cache.put(
                    self._config.repo_path, cache_key, generated.content
                )

            logger.info(
                "generated tests",
                extra={"source": source_path, "mode": mode},
            )
            produced += 1
            yield generated

        # v0.2: source files whose diff ONLY removes functions never
        # enter the loop above (no affected functions to generate tests
        # for) — but their existing test files still reference the
        # deleted symbols. Prune those tests mechanically; no LLM call
        # needed.
        for source_path, removed_here in removed_by_file.items():
            if source_path in by_file:
                continue  # already pruned inside the generation loop
            existing = existing_by_source.get(source_path)
            if existing is None:
                continue
            handler = get_handler_for_file(source_path)
            if handler is None:
                continue
            pruned = self._prune_removed(handler, existing, removed_here)
            if pruned.content == existing.content:
                continue
            logger.info(
                "pruned stale tests for removed functions (no LLM call)",
                extra={
                    "test_file": existing.test_file_path,
                    "removed_functions": [r.name for r in removed_here],
                },
            )
            produced += 1
            yield GeneratedTest(
                source_file_path=source_path,
                test_file_path=existing.test_file_path,
                content=pruned.content,
                covered_functions=[],
            )

        if failed_files:
            logger.warning(
                "test generation completed with %d failure(s) out of %d "
                "files. Successful: %d. Failed files: %s",
                len(failed_files),
                len(by_file),
                produced,
                ", ".join(path for path, _ in failed_files),
            )

        # Only raise if EVERYTHING failed (nothing yielded). Partial
        # success is worth preserving — the successful files will be
        # written to disk by the test_committer.
        if not produced and failed_files:
            first_error = failed_files[0][1]
            raise TestGeneratorError(
                f"All {len(failed_files)} file(s) failed test generation. "
                f"First error: {first_error}"
            )

    def _generate_batched(
        self,
        handler: LanguageHandler,
        source_path: str,
        functions: list[AffectedFunction],
        existing: ExistingTest | None,
    ) -> tuple[GeneratedTest, str]:
        """Generate tests for one file, splitting large diffs into
        multiple LLM calls (v0.3.0, see MAX_FUNCTIONS_PER_CALL).

        Batch 1 behaves exactly like the pre-batching code: incremental
        against a real existing test file, or fresh generation. Each
        later batch runs in incremental mode against the test file
        generated so far, and the new @Test methods are merged in.

        A failure in batch 1 propagates (nothing was generated). A
        failure in a LATER batch is non-fatal: the tests generated so
        far are kept and returned, with a warning naming the functions
        left uncovered — partial coverage beats losing completed work.
        """
        batches = _chunk(functions, MAX_FUNCTIONS_PER_CALL)
        if len(batches) > 1:
            logger.info(
                "splitting %d changed functions into %d LLM calls "
                "(max %d per call) | file=%s",
                len(functions), len(batches), MAX_FUNCTIONS_PER_CALL,
                source_path,
            )

        if existing:
            generated = self._generate_incremental(
                handler, source_path, batches[0], existing
            )
            mode = "incremental"
        else:
            generated = self._generate_fresh(
                handler, source_path, batches[0]
            )
            mode = "fresh"

        for i, batch in enumerate(batches[1:], start=2):
            synthetic = ExistingTest(
                test_file_path=generated.test_file_path,
                source_file_path=source_path,
                content=generated.content,
            )
            try:
                follow_up = self._generate_incremental(
                    handler, source_path, batch, synthetic
                )
            except TestGeneratorError as exc:
                logger.warning(
                    "batch %d/%d failed for %s — keeping the tests "
                    "generated so far; functions left uncovered: %s. "
                    "Error: %s",
                    i, len(batches), source_path,
                    ", ".join(fn.name for fn in batch), exc,
                )
                break
            generated = GeneratedTest(
                source_file_path=source_path,
                test_file_path=generated.test_file_path,
                content=follow_up.content,
                covered_functions=(
                    generated.covered_functions + follow_up.covered_functions
                ),
            )

        return generated, mode

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

        # Few-shot: show the model a real existing test from this repo so
        # it mirrors the project's actual harness (providers, custom
        # render, fixtures, base test classes) instead of guessing one.
        example_block = self._example_block(handler, source_path)
        if example_block:
            user_prompt = example_block + user_prompt

        code = self._generate_and_extract(
            handler, system_prompt, user_prompt, source_path, mode="fresh"
        )
        test_path = self._test_finder.suggest_test_path(
            source_path, existing=None
        )

        return GeneratedTest(
            source_file_path=source_path,
            test_file_path=test_path,
            content=code,
            covered_functions=[fn.qualified_name for fn in functions],
        )

    def _generate_and_extract(
        self,
        handler: LanguageHandler,
        system_prompt: str,
        user_prompt: str,
        source_path: str,
        mode: str,
    ) -> str:
        """Call the LLM and extract code, retrying ONCE if the response
        has no recognizable code.

        The 15-minute garbled ``e(true);`` response is the case this
        guards: rather than abandon the whole file after one bad
        (possibly truncated) response, regenerate once. Session-limit
        errors still propagate immediately (no point retrying).
        """
        last_exc: Exception | None = None
        for attempt in (1, 2):
            try:
                raw = self._llm.generate(system_prompt, user_prompt)
            except LLMSessionLimitError:
                raise
            except Exception as exc:
                raise TestGeneratorError(
                    f"LLM failed for {source_path}: {exc}"
                ) from exc
            try:
                code = _extract_code(
                    handler, raw, mode=mode, source_path=source_path
                )
                if not code or not code.strip():
                    raise TestGeneratorError(
                        f"LLM returned an empty test body for {source_path}"
                    )
                return code
            except TestGeneratorError as exc:
                last_exc = exc
                if attempt == 1:
                    logger.warning(
                        "unusable LLM response for %s (no recognizable "
                        "code) — regenerating once. Detail: %s",
                        source_path, exc,
                    )
        raise last_exc  # type: ignore[misc]

    def _example_block(self, handler: LanguageHandler, source_path: str) -> str:
        try:
            exclude = set(handler.candidate_test_paths(source_path))
        except Exception:
            exclude = set()
        try:
            example = self._example_finder.find_example(
                handler, source_path, exclude_paths=exclude
            )
            return format_example_block(example)
        except Exception:
            return ""

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

        new_test_code = self._generate_and_extract(
            handler, system_prompt, user_prompt, source_path,
            mode="incremental",
        ).strip()
        merged = handler.merge_new_tests(trimmed_existing, new_test_code)

        return GeneratedTest(
            source_file_path=source_path,
            test_file_path=existing.test_file_path,
            content=merged,
            covered_functions=[fn.qualified_name for fn in functions],
        )

    def _prune_removed(
        self,
        handler: LanguageHandler,
        existing: ExistingTest,
        removed: list[RemovedFunction],
    ) -> ExistingTest:
        """Strip tests covering removed source functions from
        ``existing``'s content. Purely mechanical — reuses the same
        parse/covers/remove machinery incremental mode uses to replace
        outdated tests. Returns ``existing`` unchanged when the handler
        lacks that machinery or nothing matches.
        """
        parse = getattr(handler, "parse_existing_tests", None)
        remove_tests = getattr(handler, "remove_tests", None)
        covers = getattr(handler, "covers", None)
        if not (callable(parse) and callable(remove_tests) and callable(covers)):
            return existing

        try:
            existing_tests = parse(existing.content)
        except Exception:
            return existing

        stale = [
            t
            for t in existing_tests
            if any(covers(t.name, r.name) for r in removed)
        ]
        if not stale:
            return existing

        try:
            trimmed = remove_tests(existing.content, stale)
        except Exception:
            return existing

        logger.info(
            "removing %d stale test(s) covering deleted function(s): %s",
            len(stale),
            ", ".join(sorted({t.name for t in stale})),
        )
        return ExistingTest(
            test_file_path=existing.test_file_path,
            source_file_path=existing.source_file_path,
            content=trimmed,
        )

    def _cap_functions(
        self, source_path: str, functions: list[AffectedFunction]
    ) -> list[AffectedFunction]:
        """Limit changed functions per file to
        ``config.max_functions_per_file`` (0 = unlimited).

        Prevents a large module (e.g. a Redux actions file with 28
        exports) from fanning out into many LLM calls and hundreds of
        tests nobody reviews. Kept functions are the first N in source
        order; the rest are skipped with a clear, honest log line so
        the user knows coverage was bounded and how to target the
        remainder (--file / a bigger --max-functions-per-file).
        """
        cap = getattr(self._config, "max_functions_per_file", 0) or 0
        if cap <= 0 or len(functions) <= cap:
            return functions

        kept = functions[:cap]
        skipped = [fn.name for fn in functions[cap:]]
        logger.warning(
            "capping %s: %d changed functions > --max-functions-per-file "
            "(%d). Generating for the first %d; SKIPPED %d: %s. Re-run "
            "with --file %s (or a higher --max-functions-per-file) to "
            "cover the rest.",
            source_path,
            len(functions),
            cap,
            cap,
            len(skipped),
            ", ".join(skipped),
            source_path,
        )
        return kept

    @staticmethod
    def _group_removed_by_file(
        removed: list[RemovedFunction],
    ) -> dict[str, list[RemovedFunction]]:
        groups: dict[str, list[RemovedFunction]] = {}
        for r in removed:
            groups.setdefault(r.file_path, []).append(r)
        return groups

    @staticmethod
    def _group_by_file(
        affected: list[AffectedFunction],
    ) -> dict[str, list[AffectedFunction]]:
        groups: dict[str, list[AffectedFunction]] = {}
        for fn in affected:
            groups.setdefault(fn.file_path, []).append(fn)
        return groups
