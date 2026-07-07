"""JavaScript/TypeScript (Node.js) language handler.

Implements the LanguageHandler protocol for Node.js projects using
Jest or Vitest. Design choices, and where they come from:

- Test files are COLOCATED by default (``src/utils/format.ts`` →
  ``src/utils/format.test.ts``), because that is the dominant Node
  convention (Jest's default testMatch finds ``*.test.*`` anywhere).
  ``__tests__/`` and ``tests/``-mirror layouts are covered as
  candidates and by the fallback search.

- The fallback search (find_existing_test_file_by_search) verifies a
  candidate imports the source module before using it — the same
  duplicate-file safety net the Kotlin handler grew in v0.2.0+.
  ``node_modules``, build output, and coverage dirs are never walked.

- No temp-file class renaming is needed (unlike Kotlin): duplicate
  test titles across files are legal in Jest, and the runner invokes
  the temp file by exact path (``--runTestsByPath``), so the real test
  file — if present — isn't even loaded.
"""

from __future__ import annotations

import os
import re

from test_automator._logging import get_logger
from test_automator.languages.javascript import (
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

# Directories never walked during the fallback search, and never treated
# as containing the bot's tests.
_SKIPPED_DIRS = (
    "node_modules",
    "dist",
    "build",
    "out",
    ".next",
    "coverage",
    ".git",
)

# Test-directory names that mark a path as tests even without a
# .test./.spec. infix in the filename.
_TEST_DIR_NAMES = ("__tests__", "tests", "test", "__mocks__")

_EXTENSIONS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")


class JavaScriptLanguageHandler:
    """JavaScript/TypeScript + Jest/Vitest plugin."""

    name = "javascript"
    source_extensions = _EXTENSIONS

    def __init__(self) -> None:
        self._test_dirs: list[str] = []

    def configure(self, test_dirs: list[str]) -> None:
        """Remember user-configured test dirs so the tests/-mirror
        candidate uses them. Python-style defaults ("tests", "test")
        are fine here — they're real Node conventions too.
        """
        self._test_dirs = list(test_dirs or [])

    # --- Step 2: Code analysis -------------------------------------------

    def extract_affected(
        self,
        source_code: str,
        file_path: str,
        changed_lines: set[int],
    ) -> list[AffectedFunction]:
        return analyzer.extract_affected(source_code, file_path, changed_lines)

    def extract_class_signatures(self, source_code: str) -> str:
        return analyzer.extract_class_signatures(source_code)

    # --- Step 3: Test file discovery -------------------------------------

    def suggest_test_path(self, source_path: str) -> str:
        """Colocated ``<dir>/<stem>.test.<ext>`` with the source's own
        extension (a .ts source gets a .ts test).
        """
        dir_path, filename = os.path.split(source_path)
        stem, ext = os.path.splitext(filename)
        return os.path.join(dir_path, f"{stem}.test{ext}")

    def candidate_test_paths(self, source_path: str) -> list[str]:
        """Plausible existing-test locations, priority order:

        1. colocated ``<stem>.test.<ext>``   (suggest_test_path)
        2. colocated ``<stem>.spec.<ext>``
        3. ``<dir>/__tests__/<stem>.test.<ext>``
        4. tests-root mirror: ``tests/<path-after-src>/<stem>.test.<ext>``
           (only when the source lives under ``src/``)
        """
        dir_path, filename = os.path.split(source_path)
        stem, ext = os.path.splitext(filename)

        candidates = [
            self.suggest_test_path(source_path),
            os.path.join(dir_path, f"{stem}.spec{ext}"),
            os.path.join(dir_path, "__tests__", f"{stem}.test{ext}"),
        ]

        normalized = source_path.replace(os.sep, "/")
        if normalized.startswith("src/") or "/src/" in normalized:
            after_src = normalized.split("src/", 1)[1]
            sub_dir = os.path.dirname(after_src)
            for test_root in self._test_dirs or ["tests", "test"]:
                candidates.append(
                    os.path.join(test_root, sub_dir, f"{stem}.test{ext}")
                )

        return candidates

    def find_existing_test_file_by_search(
        self, repo_path: str, source_path: str
    ) -> str | None:
        """Fallback search: walk the repo for ``<stem>.test.*`` /
        ``<stem>.spec.*`` files that actually import the source module.

        The import check prevents attributing tests of an unrelated
        module that happens to share a filename (``utils.test.ts`` is
        not rare). Skips node_modules/build/coverage dirs entirely.
        """
        stem = os.path.splitext(os.path.basename(source_path))[0]
        wanted_names = {
            f"{stem}.test{ext}" for ext in _EXTENSIONS
        } | {f"{stem}.spec{ext}" for ext in _EXTENSIONS}

        matches: list[str] = []
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [
                d for d in dirs
                if d not in _SKIPPED_DIRS and not d.startswith(".")
            ]
            for filename in files:
                if filename not in wanted_names:
                    continue
                full_path = os.path.join(root, filename)
                if self._file_imports_module(full_path, stem):
                    matches.append(os.path.relpath(full_path, repo_path))

        if not matches:
            return None

        conventional = self.suggest_test_path(source_path)
        if conventional in matches:
            return conventional

        matches.sort()
        if len(matches) > 1:
            logger.warning(
                "multiple test files found for source — picking the "
                "first alphabetically",
                extra={
                    "source": source_path,
                    "picked": matches[0],
                    "all_matches": matches,
                },
            )
        else:
            logger.warning(
                "found existing test at non-conventional path — using it "
                "instead of creating a duplicate at the conventional path",
                extra={
                    "source": source_path,
                    "expected_path": conventional,
                    "found_path": matches[0],
                },
            )
        return matches[0]

    @staticmethod
    def _file_imports_module(file_path: str, stem: str) -> bool:
        """True if the file imports/requires a module whose specifier
        ends with the source stem (``./format``, ``../utils/format``,
        ``../utils/format.js``).
        """
        try:
            with open(file_path, encoding="utf-8") as fh:
                content = fh.read()
        except OSError:
            return False

        stem_re = re.escape(stem)
        specifier = (
            rf"['\"][^'\"]*/{stem_re}(?:\.[cm]?[jt]sx?)?['\"]"
            rf"|['\"]\.?/?{stem_re}(?:\.[cm]?[jt]sx?)?['\"]"
        )
        pattern = (
            rf"(?:from\s+(?:{specifier}))"
            rf"|(?:require\(\s*(?:{specifier})\s*\))"
            rf"|(?:import\(\s*(?:{specifier})\s*\))"
            rf"|(?:jest\.mock\(\s*(?:{specifier}))"
        )
        return re.search(pattern, content) is not None

    def is_test_file(self, file_path: str) -> bool:
        if not file_path.endswith(_EXTENSIONS):
            return False
        basename = os.path.basename(file_path)
        stem = os.path.splitext(basename)[0]
        if stem.endswith(".test") or stem.endswith(".spec"):
            return True
        parts = file_path.replace(os.sep, "/").split("/")
        return any(part in _TEST_DIR_NAMES for part in parts[:-1])

    # --- Step 5: Test execution ------------------------------------------

    def build_test_command(
        self, test_files: list[str], repo_path: str
    ) -> list[str]:
        return runner.build_test_command(test_files, repo_path)

    def parse_test_output(
        self, output: str, return_code: int
    ) -> dict[str, int | bool | list[str]]:
        return runner.parse_test_output(output, return_code)

    def temp_test_file_name(self, test_file_path: str) -> str:
        """``format.test.ts`` → ``_prbot.format.test.ts``.

        Still ends in ``.test.<ext>`` so it matches Jest's/Vitest's
        default discovery patterns — belt-and-braces on top of the
        exact-path invocation.
        """
        return f"_prbot.{os.path.basename(test_file_path)}"

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
        return prompts.user_prompt_fresh(
            source_path, affected, self.suggest_test_path(source_path)
        )

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

    # --- LLM output extraction --------------------------------------------

    def extract_code(self, raw: str, mode: str) -> str:
        if mode in ("fresh", "fix"):
            return extractor.extract_js_file(raw)
        if mode == "incremental":
            return extractor.extract_js_tests_block(raw)
        raise ValueError(
            f"Unknown extraction mode {mode!r} — expected 'fresh', "
            f"'incremental', or 'fix'"
        )

    # --- Step 3 & 4 helpers -----------------------------------------------

    def parse_existing_tests(self, content: str) -> list:
        return merger.parse_existing_test_functions(content)

    def merge_new_tests(self, existing: str, new_tests: str) -> str:
        return merger.merge_new_tests(existing, new_tests)

    def extract_test_source(self, content: str, tests: list) -> str:
        return merger.extract_test_source(content, tests)

    def remove_tests(self, content: str, to_remove: list) -> str:
        return merger.remove_tests(content, to_remove)

    def covers(self, test_name: str, source_function_name: str) -> bool:
        """A test covers a function if its title starts with the exact
        function name followed by a non-identifier character (or end of
        title). Conservative on purpose — the same Option-B reasoning
        as the Kotlin handler:

        - "camelCase converts keys"      covers camelCase  ✓
        - "camelCaseDeep converts keys"  covers camelCase  ✗
        - "helper uses camelCase inside" covers camelCase  ✗
        """
        if not test_name or not source_function_name:
            return False
        clean = test_name.strip()
        if not clean.startswith(source_function_name):
            return False
        rest = clean[len(source_function_name):]
        return rest == "" or not re.match(r"[A-Za-z0-9_$]", rest)
