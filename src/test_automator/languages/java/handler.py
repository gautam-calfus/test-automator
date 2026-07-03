"""Java language handler — implements LanguageHandler for Acme.

Conventions encoded here:
- Sources at  ``src/main/java/com/acme/<path>/Foo.java``
- Tests at    ``src/test/java/com/acme/<path>/FooTest.java`` (mirrored)
- Test class  ``FooTest`` (singular ``Test`` suffix, matches Acme's
              ``UserServiceTest.java``, ``UUIDUtilsTest.java``)
- Test package: SAME as source package (NOT prefixed with ``unit.``
               like Initech Kotlin)
- Build tool: auto-detected from project root (``pom.xml`` → Maven;
              ``build.gradle*`` → Gradle; both → Maven)
- JUnit 5 + Mockito + standard JUnit assertions (not AssertJ)

The skipped-test-paths list excludes integration tests by default —
the bot's mocked unit tests don't belong in ``integration/``.
"""

from __future__ import annotations

import os
import re

from test_automator._logging import get_logger
from test_automator.languages.java import (
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


class JavaLanguageHandler:
    """Java + JUnit 5 + Mockito + Maven/Gradle plugin.

    Default conventions match Acme:
    - Sources at ``src/main/java/com/acme/<path>/Foo.java``
    - Tests at ``src/test/java/com/acme/<path>/FooTest.java``
    - Test class ``FooTest`` (singular, NOT ``Tests``)
    - Test package mirrors source package (NOT a separate ``unit.`` namespace)
    - JUnit 5 + Mockito + standard JUnit assertions (no AssertJ)
    """

    name = "java"
    source_extensions = (".java",)

    DEFAULT_SOURCE_ROOT = "src/main/java"
    DEFAULT_TEST_ROOT = "src/test/java"

    # Singular "Test" matches existing Acme files (UserServiceTest.java,
    # UUIDUtilsTest.java). Spring Boot scaffolding sometimes generates
    # "Tests" (plural) — we'll also recognize that as a fallback.
    DEFAULT_TEST_SUFFIX = "Test"

    # Test directories the bot should NEVER touch. integration/ tests
    # typically use @SpringBootTest with real beans and real DBs; the
    # bot's pure-Mockito unit tests don't belong there.
    SKIPPED_TEST_SUBDIRS = (
        "integration",
        "acceptance",
    )

    def __init__(self) -> None:
        self._source_root = self.DEFAULT_SOURCE_ROOT
        self._test_root = self.DEFAULT_TEST_ROOT
        self._test_suffix = self.DEFAULT_TEST_SUFFIX
        # Remembered from extract_class_signatures (the analysis step
        # always runs before generation) so extract_code can verify
        # generated imports against the repo index.
        self._repo_root: str | None = None
        # Remembered from build_test_command so parse_test_output can
        # fall back to JUnit XML reports for the classes just run.
        self._last_run_repo_path: str | None = None
        self._last_run_test_classes: set[str] = set()

    def configure(self, test_dirs: list[str]) -> None:
        """Honor LocalTestConfig.test_dirs if it looks Java-like."""
        if not test_dirs:
            return
        first = test_dirs[0]
        if "java" in first or "src/test" in first:
            self._test_root = first

    # --- Step 2: Code analysis -------------------------------------------

    def extract_affected(
        self,
        source_code: str,
        file_path: str,
        changed_lines: set[int],
    ) -> list[AffectedFunction]:
        return analyzer.extract_affected(source_code, file_path, changed_lines)

    def extract_class_signatures(
        self,
        source_code: str,
        source_file_path: str | None = None,
        repo_root: str | None = None,
    ) -> str:
        """Return class declaration + fields + constructor (compact),
        PLUS signatures of project-internal imported classes/enums.

        For a 5,000-line Spring service like CMService, full signatures
        with all 124 method headers would blow the prompt. Compact mode
        skips method headers — Claude sees only the class header, fields,
        and constructors. The methods being TESTED are already shown in
        full in ``functions_code``; the rest don't need to clutter the
        prompt.

        v0.3.0a6: also resolves ``import`` statements pointing to files
        IN THE REPO and includes their signatures. This fixes the class
        of bugs where Claude invents package names (``com.acme.dao``
        instead of ``com.acme.common``), adds a spurious ``Impl``
        suffix to classes that don't have one, or guesses at enum values
        (``Reason.DEPROVISION`` instead of the real
        ``Reason.ACCOUNT_DEACTIVATED``).

        When source_file_path and repo_root aren't provided (e.g. from
        older callers), falls back to the class-only signature.
        """
        own_signatures = analyzer.extract_class_signatures(
            source_code, compact=True
        )

        if repo_root:
            self._repo_root = repo_root

        if not source_file_path or not repo_root:
            return own_signatures

        try:
            from test_automator.languages.java.import_resolver import (
                format_resolved_imports_for_prompt,
                resolve_imports,
            )
            resolved = resolve_imports(
                source_code=source_code,
                source_file_path=source_file_path,
                repo_root=repo_root,
            )
            imports_block = format_resolved_imports_for_prompt(resolved)
        except Exception:
            # Import resolution is a helper, not a requirement. If it
            # fails for any reason, fall back to just the file's own
            # signatures.
            imports_block = ""

        if imports_block:
            return f"{own_signatures}\n\n{imports_block}"
        return own_signatures

    # --- Step 3: Test file discovery -------------------------------------

    def suggest_test_path(self, source_path: str) -> str:
        """Map source path to its test file.

        ``src/main/java/com/acme/service/Foo.java``
        →
        ``src/test/java/com/acme/service/FooTest.java``
        """
        relative = self._strip_source_root(source_path)
        if relative is None:
            return self._fallback_test_path(source_path)

        dir_path, filename = os.path.split(relative)
        stem, _ext = os.path.splitext(filename)
        test_filename = f"{stem}{self._test_suffix}.java"
        return os.path.join(self._test_root, dir_path, test_filename)

    def candidate_test_paths(self, source_path: str) -> list[str]:
        """All locations a test file might exist at, priority order.

        1. ``src/test/java/com/acme/x/FooTest.java`` (singular — Acme default)
        2. ``src/test/java/com/acme/x/FooTests.java`` (plural — Spring Boot scaffold)
        3. ``src/test/java/com/acme/x/FooIT.java`` (integration test — usually skipped)
        """
        primary = self.suggest_test_path(source_path)
        candidates = [primary]

        # Plural variant
        if primary.endswith(f"{self._test_suffix}.java"):
            base = primary[: -len(f"{self._test_suffix}.java")]
            candidates.append(f"{base}Tests.java")
            candidates.append(f"{base}IT.java")

        return candidates

    def find_existing_test_file_by_search(
        self, repo_path: str, source_path: str
    ) -> str | None:
        """Fallback search: walk src/test/java/ looking for ``<Foo>Test.java``
        (or ``<Foo>Tests.java``) whose contents import the source class.

        Same logic as Kotlin's search fallback but adapted for Java's
        import syntax (``import com.acme.x.Foo;``).
        """
        source_filename = os.path.basename(source_path)
        source_stem, _ = os.path.splitext(source_filename)
        # Look for both naming conventions
        test_filenames = (
            f"{source_stem}{self._test_suffix}.java",
            f"{source_stem}Tests.java",
        )

        source_class_fqn = self._derive_source_class_fqn(repo_path, source_path)
        if source_class_fqn is None:
            return None

        search_root = os.path.join(repo_path, self._test_root)
        if not os.path.isdir(search_root):
            return None

        matches: list[str] = []
        for root, dirs, files in os.walk(search_root):
            rel_root = os.path.relpath(root, repo_path)
            if any(
                f"{os.sep}{skipped}{os.sep}" in f"{os.sep}{rel_root}{os.sep}"
                or rel_root.endswith(f"{os.sep}{skipped}")
                for skipped in self.SKIPPED_TEST_SUBDIRS
            ):
                dirs[:] = []
                continue

            for test_filename in test_filenames:
                if test_filename in files:
                    full_path = os.path.join(root, test_filename)
                    if self._file_imports(full_path, source_class_fqn):
                        rel_path = os.path.relpath(full_path, repo_path)
                        matches.append(rel_path)

        if not matches:
            return None

        if len(matches) == 1:
            conventional = self.suggest_test_path(source_path)
            if matches[0] != conventional:
                logger.warning(
                    "found existing test at non-conventional path — "
                    "using it instead of creating a duplicate",
                    extra={
                        "source": source_path,
                        "expected_path": conventional,
                        "found_path": matches[0],
                    },
                )
            return matches[0]

        # Multiple matches — prefer conventional path
        conventional = self.suggest_test_path(source_path)
        if conventional in matches:
            return conventional
        matches.sort()
        logger.warning(
            "multiple test files found for source — picking the first",
            extra={"source": source_path, "matches": matches},
        )
        return matches[0]

    def _derive_source_class_fqn(
        self, repo_path: str, source_path: str
    ) -> str | None:
        """Read the source file's ``package`` line + filename to build
        the fully-qualified class name.
        """
        full_path = os.path.join(repo_path, source_path)
        try:
            with open(full_path, encoding="utf-8") as fh:
                content = fh.read(4096)
        except OSError:
            return None

        m = re.search(r"^\s*package\s+([\w.]+)\s*;", content, re.MULTILINE)
        if m is None:
            return None
        package = m.group(1)

        filename = os.path.basename(source_path)
        stem, _ = os.path.splitext(filename)
        return f"{package}.{stem}"

    @staticmethod
    def _file_imports(file_path: str, fqn: str) -> bool:
        """True if the file at ``file_path`` imports the class identified
        by ``fqn`` (either directly or via wildcard).
        """
        try:
            with open(file_path, encoding="utf-8") as fh:
                content = fh.read()
        except OSError:
            return False

        # Same package: implicit import (Java)
        package = fqn.rsplit(".", 1)[0] if "." in fqn else ""
        same_pkg = re.search(
            rf"^\s*package\s+{re.escape(package)}\s*;", content, re.MULTILINE
        )
        if same_pkg is not None:
            return True

        # Direct import: ``import com.acme.service.CMService;``
        direct_pattern = rf"^\s*import\s+{re.escape(fqn)}\s*;"
        if re.search(direct_pattern, content, re.MULTILINE):
            return True

        # Wildcard import: ``import com.acme.service.*;``
        wildcard_pattern = rf"^\s*import\s+{re.escape(package)}\.\*\s*;"
        return bool(re.search(wildcard_pattern, content, re.MULTILINE))

    def is_test_file(self, file_path: str) -> bool:
        """True if this is a Java test file (under src/test/java/ or
        with Test/Tests/IT suffix).
        """
        if not file_path.endswith(".java"):
            return False
        if "/src/test/" in file_path or file_path.startswith("src/test/"):
            return True
        name = os.path.basename(file_path)
        stem = name[: -len(".java")]
        return (
            stem.endswith("Test")
            or stem.endswith("Tests")
            or stem.endswith("IT")
        )

    def is_skipped_test_path(self, test_path: str) -> bool:
        for skip in self.SKIPPED_TEST_SUBDIRS:
            if f"/{skip}/" in test_path or test_path.startswith(f"{skip}/"):
                return True
        return False

    # --- Step 5: Test execution ------------------------------------------

    def build_test_command(
        self, test_files: list[str], repo_path: str
    ) -> list[str]:
        # Remember what we're about to run so parse_test_output can
        # read the matching JUnit XML reports if the console output
        # has no summary (plain Gradle prints none on success).
        self._last_run_repo_path = repo_path
        self._last_run_test_classes = {
            runner._path_to_class_name(p) for p in test_files
        }
        return runner.build_test_command(test_files, repo_path)

    def parse_test_output(
        self, output: str, return_code: int
    ) -> dict[str, int | bool | list[str]]:
        result = runner.parse_test_output(output, return_code)

        # v0.3.0a10: console parse found nothing at all (no summary
        # line, no compile error, exit 0). Plain Gradle without the
        # test-logger plugin is SILENT on success, so "nothing" very
        # likely means "everything passed". Confirm via the JUnit XML
        # reports for exactly the classes we just ran. On a compile
        # error or nonzero exit, ``errors`` is already 1 and we never
        # get here — stale XML can't mask a real failure.
        nothing_parsed = (
            result["passed"] == 0
            and result["failed"] == 0
            and result["errors"] == 0
        )
        if nothing_parsed and self._last_run_repo_path:
            xml_result = runner.parse_test_results_xml(
                self._last_run_repo_path, self._last_run_test_classes
            )
            if xml_result is not None:
                logger.info(
                    "console output had no test summary — using JUnit "
                    "XML reports | passed=%s failed=%s errors=%s",
                    xml_result["passed"],
                    xml_result["failed"],
                    xml_result["errors"],
                )
                return xml_result
        return result

    def temp_test_file_name(self, test_file_path: str) -> str:
        """Name for the temporary test file written during a run.

        Java requires public-class name to match filename (unlike Kotlin
        which is more lenient). So our temp file must use a transformed
        name AND the class inside must be renamed to match.

        Convention: ``CMServiceTest.java`` → ``_PRBotCMServiceTest.java``
        with the class renamed to ``_PRBotCMServiceTest``.
        """
        base = os.path.basename(test_file_path)
        return f"_PRBot{base}"

    def transform_for_temp_file(
        self, content: str, test_file_path: str
    ) -> str:
        """Rename ``class XTest`` → ``class _PRBotXTest`` to match temp
        filename (Java requires public class name = filename).
        """
        canonical_stem = os.path.splitext(os.path.basename(test_file_path))[0]
        # Find ``class XTest [extends|implements|{|<]`` and prepend prefix
        pattern = re.compile(
            rf"\bclass\s+{re.escape(canonical_stem)}\b"
        )
        return pattern.sub(
            f"class _PRBot{canonical_stem}", content, count=1
        )

    def collection_error_markers(self) -> tuple[str, ...]:
        return runner.collection_error_markers()

    # --- Step 4 & 6: LLM prompts -----------------------------------------

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
        trimmed_existing_content: str = "",
        removed_tests_code: str = "",
    ) -> str:
        return prompts.user_prompt_incremental(
            source_path, existing, affected,
            trimmed_existing_content, removed_tests_code,
        )

    def user_prompt_fix(
        self, generated: GeneratedTest, runner_output: str
    ) -> str:
        return prompts.user_prompt_fix(generated, runner_output)

    # --- LLM output extraction -------------------------------------------

    def extract_code(self, raw: str, mode: str) -> str:
        """Pull Java source out of Claude's response.

        Fresh and fix modes expect a complete file (with package, imports,
        class). Incremental mode expects just ``@Test`` method declarations.
        """
        if mode == "fresh" or mode == "fix":
            code = extractor.extract_java_file(raw)
            return self._verify_imports(code)
        if mode == "incremental":
            return extractor.extract_java_tests_block(raw)
        raise ValueError(
            f"Unknown extraction mode {mode!r} — expected 'fresh', "
            f"'incremental', or 'fix'"
        )

    def _verify_imports(self, code: str) -> str:
        """Check the generated file's project-internal imports against
        the repo-wide index and auto-correct invented packages
        (v0.3.0a10). No-op when repo_root is unknown or the index
        can't be built.
        """
        if not self._repo_root:
            return code
        try:
            from test_automator.languages.java.import_resolver import (
                verify_test_imports,
            )
            corrected, corrections = verify_test_imports(
                code, self._repo_root
            )
        except Exception:
            return code
        for correction in corrections:
            logger.info("Corrected generated import: %s", correction)
        return corrected

    # --- Existing-test parsing + merge -----------------------------------

    def parse_existing_tests(self, content: str) -> list:
        return merger.parse_existing_test_functions(content)

    def merge_new_tests(self, existing: str, new_tests: str) -> str:
        return merger.merge_new_tests(existing, new_tests)

    def extract_test_source(self, content: str, tests: list) -> str:
        return merger.extract_test_source(content, tests)

    def remove_tests(self, content: str, to_remove: list) -> str:
        return merger.remove_tests(content, to_remove)

    def covers(self, test_name: str, source_function_name: str) -> bool:
        """Conservative matcher: a test "covers" a source function if its
        name STARTS WITH ``methodName`` or ``testMethodName`` (with the
        first letter uppercased after ``test``).

        Avoids over-aggressive removal: ``serviceUsesCreate()`` does NOT
        cover ``create``, only ``createX()`` or ``testCreate()`` does.
        """
        if not test_name or not source_function_name:
            return False
        clean = test_name.strip()
        # Method-name-prefix match: shouldCreateWhenX → covers `should` (no!)
        # We use a stricter rule: test name must literally start with
        # source function name as a complete identifier (followed by
        # non-letter or end of string).
        # Match patterns:
        # 1. ``methodName...`` (e.g. ``createSavesUser`` covers ``create``)
        # 2. ``testMethodName...`` (e.g. ``testCreate`` covers ``create``)
        # 3. ``shouldMethodName...`` (e.g. ``shouldCreate`` covers ``create``)
        # 4. ``methodName_...`` (snake_case test names)
        capitalized = (
            source_function_name[0].upper() + source_function_name[1:]
            if source_function_name else ""
        )
        prefixes_to_check = (
            f"test{capitalized}",
            f"should{capitalized}",
            source_function_name,
        )
        for prefix in prefixes_to_check:
            if clean.startswith(prefix):
                # Make sure it's a complete word match (not e.g.
                # "createsOther" matching "create")
                rest = clean[len(prefix):]
                if not rest or not rest[0].islower():
                    return True
        return False

    # --- Path helpers ----------------------------------------------------

    def _strip_source_root(self, source_path: str) -> str | None:
        """Strip ``src/main/java/`` prefix. Returns None if path doesn't
        start with the source root.
        """
        root = self._source_root.rstrip("/") + "/"
        if source_path.startswith(root):
            return source_path[len(root):]
        idx = source_path.find("/" + root)
        if idx >= 0:
            return source_path[idx + 1 + len(root):]
        return None

    def _fallback_test_path(self, source_path: str) -> str:
        """When source isn't under the configured root, derive a test
        path defensively by swapping ``src/main`` → ``src/test``.
        """
        dir_path, filename = os.path.split(source_path)
        stem, _ext = os.path.splitext(filename)
        test_filename = f"{stem}{self._test_suffix}.java"
        if "src/main" in dir_path:
            test_dir = dir_path.replace("src/main", "src/test", 1)
        else:
            test_dir = self._test_root
        return os.path.join(test_dir, test_filename)
