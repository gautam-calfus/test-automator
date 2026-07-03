"""Plugin contract that every language handler must implement.

A LanguageHandler encapsulates everything that's language-specific:
- Which file extensions count as source code
- How to parse source to find changed functions/classes
- Where existing test files live
- How to run the language's test framework
- What prompts to send the LLM for test generation and fixing

The orchestrator and step files use this interface only — they never import
language-specific code directly. New languages plug in by implementing this
protocol and registering with the registry.

Stage 1 of the v0.2.0 refactor extracted the Python-specific logic out of
the step files into PythonLanguageHandler (see ``languages.python``). Stage
2 will add JavaLanguageHandler. The orchestrator's wiring doesn't change.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from test_automator.models import (
    AffectedFunction,
    ExistingTest,
    GeneratedTest,
    PRFile,
)


@runtime_checkable
class LanguageHandler(Protocol):
    """Contract every language plugin implements.

    Implementations live in ``languages.<name>.handler``. Register them with
    ``languages.registry.register_language``.
    """

    #: Short identifier ("python", "java", "kotlin", ...). Used in the CLI
    #: ``--language`` flag and in log lines.
    name: str

    #: File extensions this handler claims (e.g. (".py",) for Python,
    #: (".java",) for Java). Used by the diff reader to filter files.
    source_extensions: tuple[str, ...]

    # --- Step 2: Code analysis -------------------------------------------

    def extract_affected(
        self,
        source_code: str,
        file_path: str,
        changed_lines: set[int],
    ) -> list[AffectedFunction]:
        """Return the functions/classes/methods overlapping ``changed_lines``.

        ``source_code`` is the full file content. ``changed_lines`` is the
        1-indexed set of lines added or modified by the diff.
        """
        ...

    # --- Step 3: Test file discovery -------------------------------------

    def suggest_test_path(self, source_path: str) -> str:
        """Where this language conventionally puts the test file.

        Example for Python: ``tests/test_<stem>.py`` for ``src/foo/bar.py``.
        Example for Java:   ``src/test/java/com/foo/BarTest.java`` for
        ``src/main/java/com/foo/Bar.java``.
        """
        ...

    def candidate_test_paths(self, source_path: str) -> list[str]:
        """All locations the test file could already exist at, in priority
        order. The first one that exists on disk is treated as the canonical
        test file. The first candidate is usually the same as
        ``suggest_test_path``.
        """
        ...

    def is_test_file(self, file_path: str) -> bool:
        """True if this looks like a test file rather than source.

        Used by the diff reader to skip test files when scanning for changes
        to analyze (we don't want to "test the tests").
        """
        ...

    # --- Step 5: Test execution ------------------------------------------

    def build_test_command(
        self, test_files: list[str], repo_path: str
    ) -> list[str]:
        """Construct the subprocess args to run the given test files.

        Returned list is passed straight to ``subprocess.run``. For Python
        this is ``["python", "-m", "pytest", ...]``. For Java (Gradle) it'll
        be ``["./gradlew", "test", "--tests", "com.foo.BarTest", ...]``.
        """
        ...

    def parse_test_output(
        self, output: str, return_code: int
    ) -> dict[str, int | bool | list[str]]:
        """Parse the runner's stdout/stderr into a structured dict.

        Returns a dict with keys: ``passed``, ``failed``, ``errors``,
        ``failed_test_ids`` (list[str]), and ``is_passing`` (bool).
        """
        ...

    def temp_test_file_name(self, test_file_path: str) -> str:
        """Name to use when writing a generated test file to disk *for
        running only* (the temp version is cleaned up after the run).

        Returns just the basename. Must not collide with how the language's
        test framework discovers tests via naming conventions.
        """
        ...

    # --- Step 4 & 6: LLM prompts -----------------------------------------

    def system_prompt_fresh(self) -> str:
        """System prompt for generating a NEW test file from scratch."""
        ...

    def system_prompt_incremental(self) -> str:
        """System prompt for ADDING tests to an existing test file."""
        ...

    def system_prompt_fix(self) -> str:
        """System prompt for the failure-fix loop."""
        ...

    def user_prompt_fresh(
        self,
        source_path: str,
        affected: list[AffectedFunction],
    ) -> str:
        """User prompt body for the 'no existing tests' path."""
        ...

    def user_prompt_incremental(
        self,
        source_path: str,
        existing: ExistingTest,
        affected: list[AffectedFunction],
        trimmed_existing_content: str,
        removed_tests_code: str,
    ) -> str:
        """User prompt body for the 'merge into existing tests' path."""
        ...

    def user_prompt_fix(
        self, generated: GeneratedTest, pytest_output: str
    ) -> str:
        """User prompt body for fixing a failing test module."""
        ...

    # --- Step 3 & 4 helpers ----------------------------------------------

    def parse_existing_tests(self, content: str) -> list[ExistingTest]:
        """Return existing test functions/methods from a test file content.

        Used by the incremental merge logic to identify which existing tests
        should be replaced (because their source function was modified) and
        which should be left alone.

        Return type is a list of TestFunction-shaped objects — see the
        ``utils.test_parser`` module's ``TestFunction`` for the shape. Each
        language's handler is free to define what counts as a "test."
        """
        ...

    def merge_new_tests(self, existing: str, new_tests: str) -> str:
        """Combine the trimmed existing test file with newly-generated
        tests, producing the final on-disk content.

        Python's implementation normalizes PEP 8 spacing; Java's will
        normalize curly brace formatting; etc.
        """
        ...

    def covers(self, test_name: str, source_function_name: str) -> bool:
        """True if a test function with name ``test_name`` likely covers a
        source function with name ``source_function_name``.

        Used to identify which existing tests should be removed when their
        source function is modified.
        """
        ...

    def collection_error_markers(self) -> tuple[str, ...]:
        """Substrings in test output that signal a collection/compilation
        error (vs a normal test failure). The orchestrator uses this to skip
        the fix loop when the issue is environmental (missing dependency,
        compile error) rather than logic.
        """
        ...
