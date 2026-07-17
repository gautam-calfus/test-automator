"""Pipeline orchestrator for the local automator."""

from __future__ import annotations

from typing import Any, Callable

from test_automator._logging import get_logger
from test_automator.config import LocalTestConfig
from test_automator.languages import get_handler_for_file
from test_automator.llm_bridge import LLMBridge, create_bridge
from test_automator.models import (
    AffectedFunction,
    GeneratedTest,
    PipelineResult,
    StepOutcome,
    TestRunResult,
)
from test_automator.steps import (
    CodeAnalyzer,
    FailureFixer,
    LocalDiffReader,
    TestCommitter,
    TestFinder,
    TestGenerator,
    TestRunner,
)
from test_automator.utils.exceptions import (
    LLMSessionLimitError,
    LocalTestAutomatorError,
)

logger = get_logger(__name__)

_EMPTY_RUN = TestRunResult(
    passed=0,
    failed=0,
    errors=0,
    total=0,
    output="No tests generated.",
    failed_test_ids=[],
    is_passing=True,
)


class LocalTestPipeline:
    """Orchestrates the local test automation pipeline."""

    def __init__(
        self,
        config: LocalTestConfig,
        llm: LLMBridge | None = None,
    ) -> None:
        self._config = config
        # --llm-cmd overrides the binary for any provider; for the
        # default claude provider, fall back to the (older) dedicated
        # --claude-code-cmd flag so existing setups keep working.
        cmd_override = config.llm_cmd
        if cmd_override is None and config.llm_provider == "claude":
            cmd_override = config.claude_code_cmd
        self._llm = llm or create_bridge(
            provider=config.llm_provider,
            cmd=cmd_override,
            timeout=config.claude_code_timeout,
            max_output_tokens=config.claude_code_max_output_tokens,
            effort=getattr(config, "claude_effort", "low"),
        )
        self._reader = LocalDiffReader(config)
        self._analyzer = CodeAnalyzer(config)
        self._finder = TestFinder(config)
        self._runner = TestRunner(config)
        self._generator = TestGenerator(config, self._finder, self._llm)
        self._fixer = FailureFixer(config, self._runner, self._llm)
        self._committer = TestCommitter(config)

    def run(self) -> PipelineResult:
        steps: list[StepOutcome] = []
        tests: list[GeneratedTest] = []
        test_result: TestRunResult = _EMPTY_RUN
        commit_sha: str | None = None
        pr_url: str | None = None
        files_changed = 0

        logger.info("pipeline starting", extra={"repo": self._config.repo_path})

        pr_info, step1 = self._step(
            "local_diff_reader", lambda: self._reader.read()
        )
        steps.append(step1)
        if not step1.success or pr_info is None:
            return self._build_result(
                steps, tests, test_result, commit_sha, pr_url, files_changed,
                "", "",
            )
        files_changed = len(pr_info.files)

        if not pr_info.files:
            logger.info(
                "no eligible source files changed — done. "
                "Check that your --source-root path matches the actual "
                "directory case (e.g. 'src/main/kotlin', not "
                "'src/main/Kotlin') and that the file extensions are "
                "supported. Both committed and uncommitted working-tree "
                "changes are analyzed by default; with --committed-only, "
                "only COMMITTED changes are visible."
            )
            return self._build_result(
                steps, tests, test_result, commit_sha, pr_url, files_changed,
                pr_info.base_branch, pr_info.head_branch,
            )

        analysis, step2 = self._step(
            "code_analyzer",
            lambda: (
                self._analyzer.analyze(pr_info.files),
                # v0.2: functions deleted since the merge-base. Their
                # existing tests reference dead symbols and get pruned.
                self._analyzer.find_removed(pr_info.files),
            ),
        )
        steps.append(step2)
        affected, removed = analysis if analysis else ([], [])
        if not affected and not removed:
            logger.info("no functions affected — done")
            return self._build_result(
                steps, tests, test_result, commit_sha, pr_url, files_changed,
                pr_info.base_branch, pr_info.head_branch,
            )

        # v0.3.0a6: apply --file whitelist (if set) FIRST. When the user
        # names specific files, that trumps all other filtering.
        # Both filters only look at .file_path, so they apply to removed
        # functions the same way they apply to affected ones.
        affected = self._apply_file_whitelist(affected)
        removed = self._apply_file_whitelist(removed)
        if not affected and not removed:
            logger.info(
                "no functions remain after --file whitelist — done"
            )
            return self._build_result(
                steps, tests, test_result, commit_sha, pr_url, files_changed,
                pr_info.base_branch, pr_info.head_branch,
            )

        # v0.3.0a5: apply --java-file-filter to skip categories that
        # aren't worth generating tests for (e.g. controllers/daos when
        # the user just wants services). This runs AFTER the analyzer
        # so the diff summary remains complete and honest, but BEFORE
        # test generation so we don't waste LLM calls.
        affected = self._apply_java_file_filter(affected)
        removed = self._apply_java_file_filter(removed)
        if not affected and not removed:
            logger.info(
                "no functions remain after --java-file-filter — done"
            )
            return self._build_result(
                steps, tests, test_result, commit_sha, pr_url, files_changed,
                pr_info.base_branch, pr_info.head_branch,
            )

        existing_tests, step3 = self._step(
            # find() only reads .file_path, so removed functions ride
            # along — their source files need existing-test lookup too.
            "test_finder", lambda: self._finder.find(affected + removed)
        )
        steps.append(step3)

        # Pre-flight: if the repo's EXISTING test suite already fails to
        # compile, abort now with the errors instead of burning tokens.
        # Kotlin/Java compile all tests together, so one broken
        # pre-existing test makes every file report errors=1 and the
        # fix loop can never fix errors that live in other files.
        # Test files this run will regenerate/prune are EXPECTED to be
        # broken right now (e.g. they call a source method you just
        # removed) — the tool rewrites them, so don't let the pre-flight
        # abort on their errors. Only breakage in files the tool won't
        # touch is a real blocker.
        will_regenerate = {
            t.test_file_path for t in (existing_tests or [])
        }
        preflight_msg = self._preflight_compile_check(
            affected + removed, will_regenerate
        )
        if preflight_msg is not None:
            logger.error(preflight_msg)
            steps.append(StepOutcome(
                step="preflight_compile",
                success=False,
                message=preflight_msg,
            ))
            return self._build_result(
                steps, tests, test_result, commit_sha, pr_url, files_changed,
                pr_info.base_branch, pr_info.head_branch,
            )

        # v0.2: generation, running, and fixing are interleaved PER
        # FILE — each generated file's tests run (and get fixed)
        # immediately, before the next file is generated. Feedback is
        # instant and failures are attributed to their file by
        # construction, so the fixer never burns LLM calls on files
        # that already pass.
        gen_out, step4 = self._step(
            "test_generator",
            lambda: self._generate_run_fix(
                affected, existing_tests or [], removed
            ),
        )
        steps.append(step4)
        tests, solo_result = gen_out if gen_out else ([], None)
        if not step4.success or not tests:
            return self._build_result(
                steps, tests, test_result, commit_sha, pr_url, files_changed,
                pr_info.base_branch, pr_info.head_branch,
            )

        # Final combined run: every generated file together, as the
        # committer would write them. This is a pure pass/fail commit
        # GATE — no fixing. Every file was already generated, run, and
        # fixed in isolation inside _generate_run_fix; re-running the
        # fixer here would just repeat that work in one giant combined
        # prompt (re-sending every failing file), which is exactly the
        # token blowup v0.2's per-file flow exists to avoid. With a
        # single file the combined run equals the per-file run, so its
        # result is reused.
        test_result, step5 = self._step(
            "test_runner",
            lambda: (
                solo_result
                if solo_result is not None and len(tests) == 1
                else self._runner.run(tests)
            ),
        )
        steps.append(step5)

        commit_result, step7 = self._step(
            "test_committer",
            lambda: self._committer.commit(tests, pr_info, test_result),
        )
        if commit_result is not None:
            commit_sha, pr_url = commit_result
        steps.append(step7)

        logger.info(
            "pipeline complete",
            extra={
                # v0.2.0 fix: test_result is None when test_runner failed
                # (e.g. Gradle timeout). Treat the pipeline as failing in
                # that case rather than crashing on a NoneType attribute
                # access.
                "is_passing": (
                    test_result.is_passing if test_result is not None else False
                ),
            },
        )
        return self._build_result(
            steps, tests, test_result, commit_sha, pr_url, files_changed,
            pr_info.base_branch, pr_info.head_branch,
        )

    def _preflight_compile_check(
        self, items: list, will_regenerate: set[str] | None = None
    ) -> str | None:
        """Compile the existing test suite once per involved language
        (Kotlin/Java). Returns an actionable error message if any
        already fails to compile IN A FILE THIS RUN WON'T TOUCH, else
        None. Best-effort: languages without the hook, or an
        indeterminate result, are skipped.

        ``will_regenerate`` are test files the tool is about to rewrite
        or prune — they're allowed to be broken now (e.g. they call a
        source method you just removed); the tool fixes them. Only
        breakage OUTSIDE that set is a real blocker.
        """
        regen = {self._norm_path(p) for p in (will_regenerate or set())}
        seen: set[str] = set()
        timeout = getattr(self._config, "test_runner_timeout", 600)
        for it in items:
            handler = get_handler_for_file(getattr(it, "file_path", ""))
            if handler is None or handler.name in seen:
                continue
            seen.add(handler.name)
            check = getattr(handler, "check_tests_compile", None)
            if not callable(check):
                continue
            logger.info(
                "pre-flight: compiling existing %s test suite…",
                handler.name,
            )
            try:
                ok, output = check(self._config.repo_path, timeout)
            except Exception:
                continue
            if ok is False:
                if getattr(self._config, "repair_existing", False):
                    ok, output = self._repair_existing_tests(
                        handler, output, timeout
                    )
                    if ok:
                        logger.info(
                            "pre-flight: repaired existing %s test suite "
                            "— it now compiles; continuing",
                            handler.name,
                        )
                        continue
                # If every broken file is one this run will regenerate
                # or prune, the tool will fix them — don't abort.
                broken = {
                    self._norm_path(b) for b in self._broken_test_files(output)
                }
                external = broken - regen
                if broken and not external:
                    logger.info(
                        "pre-flight: existing %s suite doesn't compile, "
                        "but only in file(s) this run will regenerate/"
                        "prune (%s) — continuing.",
                        handler.name,
                        ", ".join(sorted(broken)),
                    )
                    continue
                errs = self._compile_error_lines(output)
                repaired_note = (
                    " (--repair-existing tried but couldn't fix all of "
                    "them)"
                    if getattr(self._config, "repair_existing", False)
                    else ""
                )
                return (
                    f"Your existing {handler.name} test suite does NOT "
                    f"compile on this branch{repaired_note} — aborting "
                    f"before spending tokens on generation.\n\n"
                    f"{handler.name.title()} compiles all tests together, "
                    f"so these pre-existing errors would make EVERY "
                    f"generated file report a build error the fix loop "
                    f"cannot repair (the errors live in other files). Fix "
                    f"or stash the broken test files (or use "
                    f"--repair-existing), then re-run.\n\n"
                    f"Compile errors:\n{errs or output[:2000]}"
                )
        return None

    @staticmethod
    def _norm_path(p: str) -> str:
        p = p.replace("\\", "/")
        return p[2:] if p.startswith("./") else p

    @staticmethod
    def _compile_error_lines(output: str) -> str:
        return "\n".join(
            ln for ln in output.splitlines()
            if ln.strip().startswith("e:")
            or ": error:" in ln
            or "error:" in ln.lower()
        )[:2000]

    def _broken_test_files(self, output: str) -> list[str]:
        """Repo-relative paths of test files with compile errors,
        parsed from Gradle/Maven/kotlinc output. De-duplicated, order
        preserved."""
        import os
        import re

        repo = os.path.abspath(self._config.repo_path)
        found: list[str] = []
        # Absolute or relative paths ending in a source extension that
        # appear on an error line.
        path_re = re.compile(r"(/?[\w./\-]+\.(?:kt|java))")
        for ln in output.splitlines():
            low = ln.lower()
            if not (ln.strip().startswith("e:") or "error" in low):
                continue
            for m in path_re.finditer(ln):
                p = m.group(1)
                ap = p if os.path.isabs(p) else os.path.join(repo, p)
                if not os.path.isfile(ap):
                    continue
                rel = os.path.relpath(ap, repo)
                # only test files
                if "/test/" in rel.replace(os.sep, "/") and rel not in found:
                    found.append(rel)
        return found

    def _repair_existing_tests(
        self, handler, output: str, timeout: int
    ) -> tuple[bool | None, str]:
        """Best-effort repair of pre-existing broken test files with the
        LLM, then re-check compilation. Bounded by max_fix_retries
        rounds. Returns the final (ok, output) from the compile check.
        """
        import os

        from test_automator.models import GeneratedTest

        rounds = max(1, getattr(self._config, "max_fix_retries", 2))
        for rnd in range(1, rounds + 1):
            broken = self._broken_test_files(output)
            if not broken:
                break
            logger.warning(
                "--repair-existing: round %d/%d — %d broken test "
                "file(s) to repair: %s",
                rnd, rounds, len(broken), broken,
            )
            for rel in broken:
                fh_handler = get_handler_for_file(rel)
                if fh_handler is None:
                    continue
                abs_p = os.path.join(self._config.repo_path, rel)
                try:
                    with open(abs_p, encoding="utf-8") as fh:
                        content = fh.read()
                except OSError:
                    continue
                gen = GeneratedTest(
                    source_file_path=rel,
                    test_file_path=rel,
                    content=content,
                    covered_functions=[],
                )
                try:
                    fixed = self._fixer._fix_one(gen, output)
                except LLMSessionLimitError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "--repair-existing: could not repair %s: %s",
                        rel, exc,
                    )
                    continue
                with open(abs_p, "w", encoding="utf-8") as fh:
                    fh.write(fixed.content)
            ok, output = handler.check_tests_compile(
                self._config.repo_path, timeout
            )
            if ok:
                return True, output
        ok, output = handler.check_tests_compile(
            self._config.repo_path, timeout
        )
        return ok, output

    def _generate_run_fix(
        self,
        affected: list[AffectedFunction],
        existing_tests: list,
        removed: list,
    ) -> tuple[list[GeneratedTest], TestRunResult | None]:
        """Generate each file's tests, then run and fix them BEFORE
        generating the next file.

        Compared to the old generate-everything-then-run-once flow:
        - a compile error is isolated to the file that caused it (the
          other files aren't even on disk yet);
        - the fix loop gets a single file's failures in its prompt and
          spends LLM calls only on files that actually fail;
        - a file that can't be fixed doesn't poison judgment of the
          rest.

        Returns ``(tests, solo_result)`` — ``solo_result`` is the last
        per-file run result when exactly one file was produced, so the
        orchestrator can skip a redundant combined run.

        Each file that passes is WRITTEN TO DISK immediately (not held
        until the end), so if the run aborts — e.g. the LLM session
        limit is hit — the completed, passing tests are already saved.
        On a session-limit abort we stop cleanly and return whatever
        was produced so far.
        """
        # Idempotency: drop files whose existing tests already pass AND
        # already cover every changed function. Without this, a second
        # run against the same base branch sees those files as "existing
        # tests to update", re-runs the LLM in incremental mode, and
        # rewrites tests that were already correct — churning passing
        # tests into different (still passing) code every run. Skipped
        # here so no LLM call is made and the file on disk is untouched.
        if not getattr(self._config, "regenerate_passing", False):
            affected = self._skip_covered_passing(affected, existing_tests)

        tests: list[GeneratedTest] = []
        last_result: TestRunResult | None = None
        total = self._expected_file_count(affected, removed)
        idx = 0

        try:
            for gen in self._generator.iter_generate(
                affected, existing_tests, removed
            ):
                idx += 1
                logger.info(
                    "[%d/%d] %s", idx, total, gen.source_file_path
                )
                result = self._runner.run([gen])
                logger.info(
                    "    run: passed=%s failed=%s errors=%s "
                    "(LLM calls so far: %s)",
                    result.passed,
                    result.failed,
                    result.errors,
                    getattr(self._llm, "calls_made", "?"),
                )
                if not result.is_passing:
                    fixed_tests, result = self._fixer.fix([gen], result)
                    if fixed_tests:
                        gen = fixed_tests[0]
                    logger.info(
                        "    after fix: passed=%s failed=%s errors=%s",
                        result.passed,
                        result.failed,
                        result.errors,
                    )
                tests.append(gen)
                last_result = result
                if result.is_passing:
                    self._persist(gen)
                    logger.info(
                        "    ✓ saved %s (%d/%d done)",
                        gen.test_file_path,
                        idx,
                        total,
                    )
                else:
                    logger.warning(
                        "    ✗ %s still failing — left for review, "
                        "not saved as final",
                        gen.test_file_path,
                    )
                # Usage readout after each file so the developer can
                # judge quota burn and stop whenever they like — every
                # passing file so far is already saved to disk, so
                # aborting (Ctrl-C) never loses completed work.
                usage = getattr(self._llm, "usage_summary", None)
                if callable(usage):
                    logger.info(
                        "    ⧗ usage so far: %s | %d/%d files done, "
                        "completed tests saved — safe to stop (Ctrl-C) "
                        "anytime",
                        usage(),
                        idx,
                        total,
                    )
        except LLMSessionLimitError as exc:
            passed_files = [t.test_file_path for t in tests]
            usage = getattr(self._llm, "usage_summary", None)
            logger.warning(
                "ABORTING run — LLM session/usage limit reached (%s). "
                "%d file(s) already generated; passing ones are saved "
                "to disk. %s",
                usage() if callable(usage) else "usage unknown",
                len(tests),
                exc,
            )
            logger.warning("Files produced before abort: %s", passed_files)

        return tests, (last_result if len(tests) == 1 else None)

    def _skip_covered_passing(
        self,
        affected: list[AffectedFunction],
        existing_tests: list,
    ) -> list[AffectedFunction]:
        """Return ``affected`` with functions removed when their source
        file's existing tests already pass AND already cover every
        changed function in that file.

        The check is all-or-nothing per file: a file is only skipped
        when EVERY one of its changed functions is already covered by a
        passing existing test. If even one changed function is
        uncovered, the file goes through normal (incremental)
        generation so the new function gets a test.

        The cheap structural check (do the existing tests name-cover the
        functions?) runs first; the expensive test run happens only for
        files that pass it — so files that will be regenerated anyway
        never pay for an extra runner invocation.
        """
        existing_by_source = {t.source_file_path: t for t in existing_tests}
        by_file: dict[str, list[AffectedFunction]] = {}
        for fn in affected:
            by_file.setdefault(fn.file_path, []).append(fn)

        kept: list[AffectedFunction] = []
        skipped: list[str] = []
        for source_path, fns in by_file.items():
            existing = existing_by_source.get(source_path)
            handler = get_handler_for_file(source_path)
            if existing is None or handler is None:
                kept.extend(fns)
                continue
            if not self._existing_covers_all(handler, existing, fns):
                kept.extend(fns)
                continue
            # Structurally covered — now confirm the existing tests
            # actually pass before deciding to leave them alone.
            probe = GeneratedTest(
                source_file_path=source_path,
                test_file_path=existing.test_file_path,
                content=existing.content,
                covered_functions=[],
            )
            try:
                result = self._runner.run([probe])
            except Exception as exc:
                logger.info(
                    "could not verify existing tests for %s (%s) — "
                    "regenerating to be safe",
                    source_path, exc,
                )
                kept.extend(fns)
                continue
            if result.is_passing:
                skipped.append(source_path)
            else:
                kept.extend(fns)

        if skipped:
            logger.info(
                "reusing %d existing test file(s) that already pass and "
                "cover the changed functions — not regenerating (pass "
                "--regenerate-passing to force): %s",
                len(skipped),
                ", ".join(sorted(skipped)),
            )
        return kept

    @staticmethod
    def _existing_covers_all(
        handler, existing, fns: list[AffectedFunction]
    ) -> bool:
        """True when every function in ``fns`` is name-covered by some
        test in ``existing``'s content. Uses the handler's own
        parse/covers machinery; returns False (i.e. "regenerate") if the
        handler lacks it or parsing fails.
        """
        parse = getattr(handler, "parse_existing_tests", None)
        covers = getattr(handler, "covers", None)
        if not (callable(parse) and callable(covers)):
            return False
        try:
            existing_tests = parse(existing.content)
        except Exception:
            return False
        for fn in fns:
            if not any(covers(t.name, fn.name) for t in existing_tests):
                return False
        return True

    @staticmethod
    def _expected_file_count(affected: list, removed: list) -> int:
        paths = {getattr(fn, "file_path", None) for fn in affected}
        paths |= {getattr(r, "file_path", None) for r in removed}
        paths.discard(None)
        return len(paths)

    def _persist(self, gen: GeneratedTest) -> None:
        """Write a passing test file to its canonical path right away so
        an abort or crash keeps the completed work. The runner uses
        backup/restore during its transient run, so nothing is on disk
        until we write it here; the final committer step is idempotent.
        """
        import os

        dest = os.path.join(self._config.repo_path, gen.test_file_path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(gen.content)

    def _step(
        self, name: str, fn: Callable[[], Any]
    ) -> tuple[Any, StepOutcome]:
        try:
            result = fn()
            return result, StepOutcome(
                step=name,
                success=True,
                message=f"{name} completed successfully",
            )
        except LocalTestAutomatorError as exc:
            logger.error(f"step {name} failed: {exc}")
            return None, StepOutcome(step=name, success=False, message=str(exc))

    def _apply_file_whitelist(self, affected: list) -> list:
        """When --file is passed one or more times, drop everything else.

        Works on any items exposing ``.file_path`` (AffectedFunction,
        RemovedFunction).

        Path comparison normalizes both sides so ``./src/main/java/Foo.java``,
        ``src/main/java/Foo.java``, and ``src\\main\\java\\Foo.java`` all
        match. Anything outside the whitelist is dropped with a summary
        log line so the user knows what got skipped.
        """
        whitelist = getattr(self._config, "file_whitelist", None)
        if not whitelist:
            return affected

        normalized_whitelist = {
            self._normalize_path(p) for p in whitelist
        }

        kept: list[AffectedFunction] = []
        skipped: list[str] = []
        for fn in affected:
            if self._normalize_path(fn.file_path) in normalized_whitelist:
                kept.append(fn)
            else:
                if fn.file_path not in skipped:
                    skipped.append(fn.file_path)

        if skipped:
            logger.info(
                "file whitelist dropped %d file(s) not in --file list. "
                "Kept: %s. Skipped: %s",
                len(skipped),
                ", ".join(whitelist),
                ", ".join(skipped[:10]) + (
                    f" ... and {len(skipped) - 10} more"
                    if len(skipped) > 10 else ""
                ),
            )
        return kept

    @staticmethod
    def _normalize_path(p: str) -> str:
        """Normalize a path for whitelist comparison. Handles ``./``
        prefix, backslash separators, and trailing slashes.
        """
        p = p.replace("\\", "/")
        if p.startswith("./"):
            p = p[2:]
        return p.rstrip("/")

    def _apply_java_file_filter(self, affected: list) -> list:
        """Filter affected functions by Java file category.

        Works on any items exposing ``.file_path`` (AffectedFunction,
        RemovedFunction).

        The filter is Java-specific. Python/Kotlin functions pass
        through unchanged. Java files matching a category the user
        did NOT request are dropped, and a summary log line records
        what was skipped so the user knows why their diff didn't
        produce as many tests as expected.

        Returns the filtered list. If no filter is configured, returns
        the input list unchanged.
        """
        file_filter = getattr(self._config, "java_file_filter", None)
        if not file_filter:
            return affected

        from test_automator.languages.java.file_filter import (
            classify_java_file, should_process_java_file,
        )

        kept: list[AffectedFunction] = []
        # Track why each file was dropped, for the summary log
        skipped: dict[str, str] = {}
        for fn in affected:
            if not fn.file_path.endswith(".java"):
                # Non-Java files always pass through
                kept.append(fn)
                continue
            if should_process_java_file(fn.file_path, file_filter):
                kept.append(fn)
            else:
                category = classify_java_file(fn.file_path) or "unknown"
                skipped[fn.file_path] = category

        if skipped:
            logger.info(
                "java file filter dropped %d file(s) not matching "
                "categories %s. Skipped: %s",
                len(skipped),
                ", ".join(file_filter),
                ", ".join(
                    f"{path} ({cat})" for path, cat in skipped.items()
                ),
            )
        return kept

    def _build_result(
        self,
        steps: list[StepOutcome],
        tests: list[GeneratedTest],
        test_result: TestRunResult,
        commit_sha: str | None,
        pr_url: str | None,
        files_changed: int,
        base_branch: str,
        head_branch: str,
    ) -> PipelineResult:
        # Overall result is passing only if BOTH conditions hold:
        # 1. Every step that ran completed successfully (no ✗ in the steps
        #    list). This catches cases like test_generator failing with
        #    NotImplementedError for a language whose Stage 4 isn't done.
        # 2. The tests themselves passed (test_result.is_passing).
        #
        # Previously this only checked condition 2, which meant a run that
        # failed at test_generator (so no tests ever ran) would still
        # report PASS because the never-run test result is initialized to
        # is_passing=True. That was misleading and is now fixed.
        all_steps_ok = all(step.success for step in steps)

        return PipelineResult(
            repo_path=self._config.repo_path,
            base_branch=base_branch,
            head_branch=head_branch,
            files_changed=files_changed,
            functions_affected=sum(
                len(t.covered_functions) for t in (tests or [])
            ),
            tests_generated=len(tests or []),
            test_result=test_result,
            commit_sha=commit_sha,
            pr_url=pr_url,
            steps=steps,
            is_passing=(
                all_steps_ok
                and test_result is not None
                and test_result.is_passing
            ),
            llm_usage=(
                self._llm.usage_summary()
                if hasattr(self._llm, "usage_summary")
                else ""
            ),
        )
