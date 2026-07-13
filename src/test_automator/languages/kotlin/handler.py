"""Kotlin language handler — Stage 2 skeleton.

Implements the LanguageHandler protocol. Stage 2 supplies:
- name, source_extensions
- extract_affected (real tree-sitter-kotlin parsing)
- is_test_file (recognize *Tests.kt and src/test/kotlin layout)
- candidate_test_paths and suggest_test_path (mirrors src/main → src/test)
- temp_test_file_name (so writes don't collide)
- collection_error_markers (so the fix loop bails on compile errors)

Stages 3-5 will fill in:
- build_test_command       (Stage 3 — Gradle subprocess)
- parse_test_output        (Stage 3 — Gradle output parsing)
- system_prompt_*          (Stage 4 — Strikt + MockK + backticked names)
- user_prompt_*            (Stage 4)
- parse_existing_tests     (Stage 4 — pull out fun `backticked name`)
- merge_new_tests          (Stage 4)

Until those land, the unimplemented methods raise NotImplementedError
with messages pointing at which stage delivers them. The orchestrator
will surface those errors as failed step outcomes.

Defaults baked into this handler match Initech's accounts-service conventions:
- src/main/kotlin → src/test/kotlin/unit/...
- ``ClassName`` → ``ClassNameTests.kt`` (plural "Tests" suffix)
- Tests under ``src/test/kotlin/acceptance/`` are skipped (Cucumber/BDD,
  not our domain)
"""

from __future__ import annotations

import os
import re

from test_automator._logging import get_logger
from test_automator.languages.kotlin import (
    analyzer,
    extractor,
    merger,
    prompts,
    runner,
)
from test_automator.models import (
    AffectedFunction,
    ExistingTest,
    GeneratedTest,
)

logger = get_logger(__name__)


class KotlinLanguageHandler:
    """Kotlin + JUnit 5 + MockK + Strikt + Gradle plugin.

    Default conventions match Initech accounts-service:
    - Sources at ``src/main/kotlin/<pkg>/Foo.kt``
    - Unit tests at ``src/test/kotlin/unit/<pkg>/FooTests.kt``
    - Tests under ``src/test/kotlin/acceptance/`` and Cucumber feature
      files are NOT touched (they're human-written BDD tests)
    """

    name = "kotlin"
    source_extensions = (".kt",)

    DEFAULT_SOURCE_ROOT = "src/main/kotlin"
    DEFAULT_TEST_ROOT = "src/test/kotlin"
    DEFAULT_TEST_SUBDIR = "unit"  # tests land under unit/, not integration/

    # Suffix appended to the source class name to form the test class name.
    # Initech uses plural "Tests" (e.g. UserServiceTests).
    DEFAULT_TEST_SUFFIX = "Tests"

    # Test directories the bot should NEVER touch. Cucumber/acceptance tests
    # are human-written BDD specs; the bot doesn't generate those.
    # Test directories the bot should NEVER touch. Cucumber/acceptance tests
    # are human-authored BDD tests where the bot has no business making
    # changes; integration tests run against real Spring application context
    # and use a fundamentally different testing paradigm (real beans, real
    # DB, real HTTP) from the MockK unit tests the bot is designed for.
    SKIPPED_TEST_SUBDIRS = (
        "acceptance",
        "acceptance/features",
        # v0.2.1: integration tests excluded after the bot modified an
        # integration test file because the search fallback didn't skip
        # them. Integration tests are humans' job — the bot only owns
        # the unit/ tree.
        "integration",
    )

    def __init__(self) -> None:
        self._source_root = self.DEFAULT_SOURCE_ROOT
        self._test_root = self.DEFAULT_TEST_ROOT
        self._test_subdir = self.DEFAULT_TEST_SUBDIR
        self._test_suffix = self.DEFAULT_TEST_SUFFIX

    def configure(self, test_dirs: list[str]) -> None:
        """Allow LocalTestConfig.test_dirs to override the test root.

        If the caller passes a Kotlin-style test root (containing "kotlin"
        or "src/test"), use it. Otherwise leave the defaults alone — the
        Python-style "tests"/"test" values are meaningless for Kotlin.
        """
        if not test_dirs:
            return
        first = test_dirs[0]
        if "kotlin" in first or "src/test" in first:
            self._test_root = first

    # --- Step 2: Code analysis -------------------------------------------

    def extract_affected(
        self,
        source_code: str,
        file_path: str,
        changed_lines: set[int],
    ) -> list[AffectedFunction]:
        return analyzer.extract_affected(
            source_code, file_path, changed_lines
        )

    def extract_class_signatures(
        self,
        source_code: str,
        source_file_path: str | None = None,
        repo_root: str | None = None,
    ) -> str:
        """Return class/data class/interface signatures from the file
        (no bodies), PLUS — when the analyzer passes
        source_file_path/repo_root — the resolved signatures of the
        project classes this file imports.

        This is the v0.2.0a6.post4 fix for the "Claude hallucinates
        constructor parameters" problem, now extended across files: the
        model sees not just THIS file's signatures but the real
        constructor/method signatures of every project class it
        imports, so it never guesses e.g. ``SalesforceService(clientId
        =..., clientSecret=...)`` when the real ctor takes ``(config,
        authenticator)``.
        """
        own = analyzer.extract_class_signatures(source_code)
        if not source_file_path or not repo_root:
            return own
        try:
            from test_automator.languages.kotlin.import_resolver import (
                resolve_imports_block,
            )
            imported = resolve_imports_block(
                source_code, source_file_path, repo_root
            )
        except Exception:
            imported = ""
        if imported and own.strip():
            return f"{own}\n\n{imported}"
        return imported or own

    # --- Step 3: Test file discovery -------------------------------------

    def suggest_test_path(self, source_path: str) -> str:
        """Map src/main/kotlin/com/initech/accounts/services/Bar.kt
            → src/test/kotlin/unit/services/BarTests.kt.

        Initech convention: tests live in a ``unit/<sub-path>`` directory
        where <sub-path> is everything after the ``com/initech/accounts/``
        prefix in the source path. The matching package declaration is
        ``package unit.services.<sub-path>`` (not ``com.initech.accounts.
        <sub-path>``).

        For non-Initech sources (where the path doesn't have the prefix),
        we keep the full path under ``unit/`` as a fallback.
        """
        relative = self._strip_source_root(source_path)
        if relative is None:
            return self._fallback_test_path(source_path)

        # Strip the com/initech/accounts/ prefix so tests land at
        # unit/services/... not unit/com/initech/accounts/services/...
        initech_prefix = "com/initech/accounts/"
        if relative.startswith(initech_prefix):
            relative = relative[len(initech_prefix) :]

        dir_path, filename = os.path.split(relative)
        stem, _ext = os.path.splitext(filename)
        test_filename = f"{stem}{self._test_suffix}.kt"
        return os.path.join(
            self._test_root, self._test_subdir, dir_path, test_filename
        )

    def candidate_test_paths(self, source_path: str) -> list[str]:
        """Three plausible test file locations, priority order:

        1. ``src/test/kotlin/unit/<pkg>/XTests.kt`` (Initech default)
        2. ``src/test/kotlin/<pkg>/XTests.kt`` (no unit/ subdir)
        3. ``src/test/kotlin/unit/<pkg>/XTest.kt`` (singular variant)

        The test_finder probes each in order. If none exist on disk,
        it falls back to ``find_existing_test_file_by_search`` below.
        """
        primary = self.suggest_test_path(source_path)
        candidates = [primary]

        # Variant 2: without the unit/ subdir
        if f"/{self._test_subdir}/" in primary:
            no_subdir = primary.replace(
                f"/{self._test_subdir}/", "/", 1
            )
            candidates.append(no_subdir)

        # Variant 3: singular "Test" suffix
        if primary.endswith(f"{self._test_suffix}.kt"):
            base = primary[: -len(f"{self._test_suffix}.kt")]
            candidates.append(f"{base}Test.kt")

        return candidates

    def find_existing_test_file_by_search(
        self, repo_path: str, source_path: str
    ) -> str | None:
        """Fallback search: walk ``src/test/kotlin/`` looking for a file
        named ``<SourceClass>Tests.kt`` whose contents import the source
        class being tested.

        This is the v0.2.0+ safety improvement for the case where a test
        file exists at a non-conventional path. Without this, the bot
        would silently create a duplicate file at the conventional path
        — leaving two ``<SourceClass>Tests`` classes in different
        packages, fragmenting the test suite.

        Algorithm:
        1. Derive the expected test filename: ``Foo.kt`` → ``FooTests.kt``
        2. Derive the source class's fully-qualified name from its
           package declaration + filename
        3. Walk ``src/test/kotlin/`` (skipping acceptance/integration
           subdirs) looking for files matching the test filename
        4. For each match, verify it imports the source class —
           otherwise we'd risk attributing tests of an unrelated
           ``Foo`` to this source
        5. If exactly one valid match → use it
        6. If multiple matches → prefer the conventional path, otherwise
           pick alphabetically first and log a warning
        7. If no matches → return None (caller falls back to fresh
           generation at the conventional path)

        Returns the path relative to ``repo_path``, or None.
        """
        # Step 1: expected test filename
        source_filename = os.path.basename(source_path)
        source_stem, _ = os.path.splitext(source_filename)
        test_filename = f"{source_stem}{self._test_suffix}.kt"

        # Step 2: source class FQN for import verification
        source_class_fqn = self._derive_source_class_fqn(
            repo_path, source_path
        )
        if source_class_fqn is None:
            # Can't read the source file or can't find its package.
            # Without the FQN, we can't safely match by filename alone
            # (would risk picking up tests of an unrelated Foo class).
            return None

        # Step 3-4: walk the test root, collect candidates that import
        # the source class
        search_root = os.path.join(repo_path, self._test_root)
        if not os.path.isdir(search_root):
            return None

        matches: list[str] = []
        for root, dirs, files in os.walk(search_root):
            # Skip acceptance/ and any other subdirs the bot ignores.
            # Compare against the relative path from repo root for safety.
            rel_root = os.path.relpath(root, repo_path)
            if any(
                f"{os.sep}{skipped}{os.sep}" in f"{os.sep}{rel_root}{os.sep}"
                or rel_root.endswith(f"{os.sep}{skipped}")
                or rel_root.split(os.sep, 2)[-1].startswith(skipped)
                for skipped in self.SKIPPED_TEST_SUBDIRS
            ):
                # Don't descend further into skipped directories
                dirs[:] = []
                continue

            if test_filename in files:
                full_path = os.path.join(root, test_filename)
                if self._file_imports(full_path, source_class_fqn):
                    rel_path = os.path.relpath(full_path, repo_path)
                    matches.append(rel_path)

        if not matches:
            return None

        # Step 5: exactly one match
        if len(matches) == 1:
            conventional = self.suggest_test_path(source_path)
            if matches[0] != conventional:
                logger.warning(
                    "found existing test at non-conventional path — "
                    "using it instead of creating a duplicate at the "
                    "conventional path",
                    extra={
                        "source": source_path,
                        "expected_path": conventional,
                        "found_path": matches[0],
                    },
                )
            return matches[0]

        # Step 6: multiple matches — prefer the conventional path
        conventional = self.suggest_test_path(source_path)
        if conventional in matches:
            logger.warning(
                "multiple test files found for source — using the one "
                "at the conventional path",
                extra={
                    "source": source_path,
                    "picked": conventional,
                    "all_matches": matches,
                },
            )
            return conventional

        # No conventional match among multiples — prefer matches under
        # the unit/ subdir (where the bot is designed to operate) over
        # any others. v0.2.1 fix: without this preference, alphabetical
        # ordering would pick ``integration/`` before ``unit/``, and the
        # bot would modify integration tests instead of unit tests.
        unit_matches = [
            m for m in matches
            if f"/{self._test_subdir}/" in f"/{m}"
            or m.startswith(f"{self._test_subdir}/")
            or f"/{self._test_root}/{self._test_subdir}/" in f"/{m}"
        ]
        if unit_matches:
            unit_matches.sort()
            picked = unit_matches[0]
            logger.warning(
                "multiple test files found for source but none at the "
                "conventional path — picking the first match under "
                f"{self._test_subdir}/",
                extra={
                    "source": source_path,
                    "expected_path": conventional,
                    "picked": picked,
                    "all_matches": matches,
                },
            )
            return picked

        # No unit/ match either — pick alphabetically first
        matches.sort()
        logger.warning(
            "multiple test files found for source but none at the "
            "conventional path or under the unit/ subdir — picking the "
            "first alphabetically",
            extra={
                "source": source_path,
                "expected_path": conventional,
                "picked": matches[0],
                "all_matches": matches,
            },
        )
        return matches[0]

    def _derive_source_class_fqn(
        self, repo_path: str, source_path: str
    ) -> str | None:
        """Read the source file's ``package`` declaration and combine
        with the class name (from the filename) to get the
        fully-qualified class name.

        Example: ``src/main/kotlin/com/initech/accounts/services/user/
        UserService.kt`` with ``package com.initech.accounts.services.user``
        → ``com.initech.accounts.services.user.UserService``.

        Returns None if the file can't be read or has no package
        declaration in the first 2KB. We only read the head of the file
        because the package is always near the top.
        """
        full_source_path = os.path.join(repo_path, source_path)
        try:
            with open(full_source_path, encoding="utf-8") as fh:
                head = fh.read(2048)
        except OSError:
            return None

        pkg_match = re.search(
            r"^\s*package\s+([\w.]+)", head, re.MULTILINE
        )
        if not pkg_match:
            return None

        pkg = pkg_match.group(1)
        stem = os.path.splitext(os.path.basename(source_path))[0]
        return f"{pkg}.{stem}"

    @staticmethod
    def _file_imports(file_path: str, fqn: str) -> bool:
        """True if the file at ``file_path`` contains an ``import`` of
        the given fully-qualified class name.

        Matches both plain imports (``import com.foo.Bar``) and aliased
        imports (``import com.foo.Bar as MyBar``). Wildcard imports
        (``import com.foo.*``) are also considered a match — they pull
        in the entire package including the class.
        """
        try:
            with open(file_path, encoding="utf-8") as fh:
                content = fh.read()
        except OSError:
            return False

        # Match: "import com.initech.accounts.services.user.UserService"
        # or:    "import com.initech.accounts.services.user.UserService as Foo"
        plain_pattern = (
            rf"^\s*import\s+{re.escape(fqn)}\s*(?:as\s+\w+)?\s*$"
        )
        if re.search(plain_pattern, content, re.MULTILINE):
            return True

        # Match wildcard import of the parent package:
        # "import com.initech.accounts.services.user.*"
        package_part = fqn.rsplit(".", 1)[0]
        wildcard_pattern = (
            rf"^\s*import\s+{re.escape(package_part)}\.\*\s*$"
        )
        return bool(re.search(wildcard_pattern, content, re.MULTILINE))

    def is_test_file(self, file_path: str) -> bool:
        """True if this looks like a Kotlin test file."""
        if not file_path.endswith(".kt"):
            return False

        # Tests under src/test/kotlin/
        if "/src/test/" in file_path or file_path.startswith("src/test/"):
            return True

        # Or files whose name ends with Test/Tests/IT
        name = os.path.basename(file_path)
        stem = name[: -len(".kt")]
        return (
            stem.endswith("Test")
            or stem.endswith("Tests")
            or stem.endswith("IT")
        )

    def is_skipped_test_path(self, test_path: str) -> bool:
        """True if a test file lives in a subdirectory the bot should not
        touch (e.g. acceptance/ Cucumber tests).
        """
        for skip in self.SKIPPED_TEST_SUBDIRS:
            if f"/{skip}/" in test_path or test_path.startswith(f"{skip}/"):
                return True
            if f"/{self._test_root}/{skip}/" in f"/{test_path}":
                return True
        return False

    # --- Step 5: Test execution -----------------------------------------
    # Stage 3 deliverables — uses the ``runner`` module which was written
    # against real Gradle output from accounts-service. Handles three
    # scenarios: passing tests, failing tests, and compile errors.

    def build_test_command(
        self, test_files: list[str], repo_path: str
    ) -> list[str]:
        return runner.build_test_command(test_files, repo_path)

    def parse_test_output(
        self, output: str, return_code: int
    ) -> dict[str, int | bool | list[str]]:
        return runner.parse_test_output(output, return_code)

    def temp_test_file_name(self, test_file_path: str) -> str:
        """Name for the temporary test file we write during a run.

        Critical detail for Kotlin: the test class inside this file MUST
        be renamed to match. Two Kotlin files in the same package both
        declaring ``class XTests`` will cause a compile conflict (Kotlin
        does NOT require file-name and class-name to match, but it DOES
        forbid duplicate class names in a package).

        Stage 4's prompts handle the class-name renaming. The bot will
        instruct Claude to name the generated class ``_PRBotXTests`` to
        match this file name. For now (Stage 3), the runner uses this
        name to derive the ``--tests`` argument for Gradle.

        Example: UserServiceTests.kt -> _PRBotUserServiceTests.kt
        """
        base = os.path.basename(test_file_path)
        return f"_PRBot{base}"

    def collection_error_markers(self) -> tuple[str, ...]:
        return runner.collection_error_markers()

    def check_tests_compile(
        self, repo_path: str, timeout: int = 600
    ) -> tuple[bool | None, str]:
        return runner.check_tests_compile(repo_path, timeout)

    # --- Step 4 & 6: LLM prompts ----------------------------------------
    # Stage 4a deliverables — fresh generation works. Stage 4b (incremental
    # merge and failure-fix loop) is still pending; those methods delegate
    # to ``prompts`` which raises NotImplementedError with a clear message.

    def system_prompt_fresh(self) -> str:
        return prompts.SYSTEM_PROMPT_FRESH

    def system_prompt_incremental(self) -> str:
        return prompts.SYSTEM_PROMPT_INCREMENTAL

    def system_prompt_fix(self) -> str:
        return prompts.SYSTEM_PROMPT_FIX

    def user_prompt_fresh(
        self, source_path: str, affected: list[AffectedFunction]
    ) -> str:
        return prompts.user_prompt_fresh(source_path, affected)

    def user_prompt_incremental(
        self,
        source_path: str,
        existing: ExistingTest,
        affected: list[AffectedFunction],
        trimmed_existing_content: str,
        removed_tests_code: str,
    ) -> str:
        return prompts.user_prompt_incremental(
            source_path,
            existing,
            affected,
            trimmed_existing_content,
            removed_tests_code,
        )

    def user_prompt_fix(
        self, generated: GeneratedTest, runner_output: str
    ) -> str:
        return prompts.user_prompt_fix(generated, runner_output)

    # --- Content transformations for temp file / commit ------------------
    # When the bot runs a Kotlin test, it has to use a temp file name
    # (``_PRBotXTests.kt``) to avoid a duplicate-class compile error with
    # any existing real ``XTests.kt``. The class inside the temp file
    # must match the temp file name.
    #
    # Claude generates content with the CANONICAL class name (``class
    # XTests``). The transformation below is applied when the runner
    # writes the temp file — it adds the ``_PRBot`` prefix to the class
    # declaration to match the temp filename.
    #
    # After the test passes, the committer writes the original content
    # (with canonical class name) to the canonical file path. No further
    # transformation needed.

    def transform_for_temp_file(
        self, content: str, test_file_path: str
    ) -> str:
        """Rename ``class XTests`` to ``class _PRBotXTests`` so the
        temp-file write doesn't conflict with any existing real XTests
        in the same package.

        ``test_file_path`` is the CANONICAL path (``...XTests.kt``); we
        derive the canonical class stem from its filename, then add the
        ``_PRBot`` prefix.
        """
        import os
        canonical_stem = os.path.splitext(
            os.path.basename(test_file_path)
        )[0]
        return prompts.rename_class_to_temp_form(content, canonical_stem)

    # --- LLM output extraction (Kotlin-specific) -------------------------
    # The default Python ``extract_code_block`` only handles markdown
    # fences. Kotlin LLM responses often have prose preambles ("Let me
    # generate the tests..."), prose postambles ("Notes on what each
    # group covers..."), or both, with or without markdown fences. This
    # hook handles all of those.

    def extract_code(self, raw: str, mode: str) -> str:
        """Extract clean Kotlin from a raw LLM response.

        ``mode='fresh'`` extracts a complete file (anchored on the
        ``package`` line through to the class's closing brace).
        ``mode='incremental'`` extracts ``@Test`` declarations to be
        spliced into an existing class.

        Raises ``ExtractionError`` if no usable Kotlin is found in the
        response — the test_generator catches this and surfaces it as a
        clear TestGeneratorError so the user sees what happened rather
        than getting prose written to disk.
        """
        if mode == "fresh" or mode == "fix":
            # Fix prompts also return a complete file
            return extractor.extract_kotlin_file(raw)
        if mode == "incremental":
            return extractor.extract_kotlin_tests_block(raw)
        raise ValueError(
            f"Unknown extraction mode {mode!r} — expected 'fresh', "
            f"'incremental', or 'fix'"
        )

    # --- Step 3 & 4 helpers (Stage 4b) -----------------------------------

    def parse_existing_tests(self, content: str) -> list:
        """Return the list of ``@Test`` functions in an existing test file.

        Each element is a ``KotlinTestFunction`` dataclass with name,
        line_start, line_end, and annotations fields. The protocol types
        this as ``list[ExistingTest]`` for forward compatibility but the
        actual element shape is the AST-level dataclass.
        """
        return merger.parse_existing_test_functions(content)

    def merge_new_tests(self, existing: str, new_tests: str) -> str:
        """Insert new ``@Test`` declarations into the existing class
        body's closing-brace position.

        ``new_tests`` is a string of just the new test methods (no
        package/imports/class wrapper) — that's what
        ``SYSTEM_PROMPT_INCREMENTAL`` asks Claude to produce.
        """
        return merger.merge_new_tests(existing, new_tests)

    # These helpers aren't in the LanguageHandler protocol — they're
    # called directly by the TestGenerator step. Same pattern as the
    # Python handler.

    def extract_test_source(
        self, content: str, tests: list
    ) -> str:
        """Return verbatim source text for the given tests, in file order.

        Used by TestGenerator to show Claude what was removed (so it has
        a hint at the prior style for the same source functions).
        """
        return merger.extract_test_source(content, tests)

    def remove_tests(self, content: str, to_remove: list) -> str:
        """Return the file content with the specified tests removed.

        Preserves imports, class-level mocks/slots, ``@BeforeEach``, and
        all other tests.
        """
        return merger.remove_tests(content, to_remove)

    def covers(self, test_name: str, source_function_name: str) -> bool:
        """Conservative ``covers`` matcher (Option B from Stage 4b design).

        A test "covers" a source function if its backticked-English name
        STARTS WITH ``functionName(`` (the open-paren is the disambiguator).

        Initech's convention is ``fun `methodName() does X``` — so we
        require the test name to start with the source function name
        immediately followed by ``(``. This avoids over-aggressive
        removal:
        - ``create() saves new user`` covers ``create`` ✓
        - ``serviceMethod also uses create internally`` covers ``create`` ✗
          (the source function name doesn't start the test name with a
          following paren)

        For camelCase pytest-style names (``testCreate``), the prefix
        ``test`` followed by capitalized source name is also accepted —
        though Initech doesn't appear to use this style in their Kotlin
        tests.
        """
        if not test_name or not source_function_name:
            return False

        # Strip surrounding backticks if present (defensive — parser
        # already strips them, but this method is also called from
        # outside)
        clean = test_name.strip()
        if clean.startswith("`") and clean.endswith("`"):
            clean = clean[1:-1]

        # Conservative: must start with "functionName("
        # The trailing ``(`` is what distinguishes ``create(...)``
        # from ``createOther(...)`` or ``aMethod includes create``.
        prefix = f"{source_function_name}("
        if clean.startswith(prefix):
            return True

        # Also accept the camelCase "testFooBar" style as a courtesy
        # (some Initech tests may use this older form). Requires exact
        # ``test`` prefix followed by Capitalized source name.
        if clean.lower().startswith("test") and source_function_name:
            suffix = clean[len("test") :]
            if suffix.startswith(
                source_function_name[0].upper() + source_function_name[1:]
            ):
                return True

        return False

    # ---------------------------------------------------------------------
    # Internal: source-path manipulation
    # ---------------------------------------------------------------------

    def _strip_source_root(self, source_path: str) -> str | None:
        """If source_path starts with src/main/kotlin/, return the part
        after that. Otherwise None.
        """
        root = self._source_root.rstrip("/") + "/"
        if source_path.startswith(root):
            return source_path[len(root) :]
        idx = source_path.find("/" + root)
        if idx >= 0:
            return source_path[idx + 1 + len(root) :]
        return None

    def _fallback_test_path(self, source_path: str) -> str:
        """Heuristic fallback when source isn't under the configured root.

        Replaces 'src/main' with 'src/test' in the directory portion and
        appends the configured test suffix to the file stem.
        """
        dir_path, filename = os.path.split(source_path)
        stem, _ext = os.path.splitext(filename)
        test_filename = f"{stem}{self._test_suffix}.kt"

        if "src/main" in dir_path:
            test_dir = dir_path.replace("src/main", "src/test", 1)
        else:
            test_dir = os.path.join(self._test_root, self._test_subdir)

        return os.path.join(test_dir, test_filename)
