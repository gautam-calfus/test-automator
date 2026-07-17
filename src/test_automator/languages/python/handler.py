"""Python language handler — the only built-in plugin in v0.2.0.

Implements the LanguageHandler protocol by delegating to the
``languages.python`` submodules (analyzer, finder, runner, prompts). The
behavior of every method is identical to the pre-refactor implementation in
the ``steps`` package.
"""

from __future__ import annotations

from test_automator.languages.python import (
    analyzer,
    finder,
    prompts,
    runner,
)
from test_automator.models import (
    AffectedFunction,
    ExistingTest,
    GeneratedTest,
)


class PythonLanguageHandler:
    """Python+pytest plugin."""

    name = "python"
    source_extensions = (".py",)
    # Line-comment token for the embedded idempotency manifest (Python
    # uses '#'; the default for other handlers is '//').
    manifest_comment = "#"
    # Indentation is semantic in Python, so the formatting-only change
    # detector must keep leading-indent when comparing function bodies
    # (a re-indent can change control flow).
    indent_significant = True

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
        """Resolve the file's project-internal imports to their real
        signatures from the codebase, so the model uses exact
        names/params instead of guessing. Returns "" when nothing
        resolves (e.g. only stdlib/third-party imports). The analyzer
        passes source_file_path/repo_root because this signature
        declares them.
        """
        if not source_file_path or not repo_root:
            return ""
        try:
            from test_automator.languages.python.import_resolver import (
                resolve_imports_block,
            )
            return resolve_imports_block(
                source_code, source_file_path, repo_root
            )
        except Exception:
            return ""

    # --- Step 3: Test file discovery -------------------------------------

    def __init__(self, test_dirs: list[str] | None = None) -> None:
        # test_dirs may be passed in from config so suggest_test_path picks
        # the user's preferred directory. Defaults match the original code.
        self._test_dirs = test_dirs or ["tests", "test"]
        # How to invoke pytest: "auto" (uv when the project is uv-managed,
        # else plain python), "uv", or "pip". Set from config via
        # set_python_runner before the runner builds its command.
        self._python_runner = "auto"

    def configure(self, test_dirs: list[str]) -> None:
        """Update test_dirs at runtime. Called by the orchestrator after
        reading LocalTestConfig.
        """
        self._test_dirs = list(test_dirs)

    def set_python_runner(self, python_runner: str) -> None:
        """Record the user's pytest-invocation preference (auto/uv/pip).

        Optional hook (not part of the LanguageHandler protocol): the
        TestRunner calls it via getattr before building the command so a
        uv-managed project runs tests through ``uv run``.
        """
        self._python_runner = python_runner or "auto"

    def suggest_test_path(self, source_path: str) -> str:
        return finder.suggest_test_path(source_path, self._test_dirs)

    def candidate_test_paths(self, source_path: str) -> list[str]:
        return finder.candidate_test_paths(source_path, self._test_dirs)

    def is_test_file(self, file_path: str) -> bool:
        return finder.is_test_file(file_path)

    # --- Step 5: Test execution ------------------------------------------

    def build_test_command(
        self, test_files: list[str], repo_path: str
    ) -> list[str]:
        return runner.build_test_command(
            test_files, repo_path, self._python_runner
        )

    def parse_test_output(
        self, output: str, return_code: int
    ) -> dict[str, int | bool | list[str]]:
        return runner.parse_test_output(output, return_code)

    def temp_test_file_name(self, test_file_path: str) -> str:
        return finder.temp_test_file_name(test_file_path)

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
        self, generated: GeneratedTest, pytest_output: str
    ) -> str:
        return prompts.user_prompt_fix(generated, pytest_output)

    # --- LLM output extraction -------------------------------------------

    def extract_code(self, raw: str, mode: str) -> str:
        """Pull Python out of the LLM response and VALIDATE it parses.

        The generic markdown extractor happily returns prose or a
        truncated fragment as "code"; a malformed/garbled response then
        gets written and blows up only at pytest time. Parsing with
        ``ast`` here turns that into a clean extraction failure, which
        the generator retries once before giving up — so a single bad
        (e.g. truncated) response doesn't cost the whole file.
        """
        import ast

        from test_automator.utils.diff_parser import extract_code_block

        code = extract_code_block(raw)
        if not code or not code.strip():
            raise ValueError("no Python code found in LLM response")
        try:
            ast.parse(code)
        except SyntaxError as exc:
            raise ValueError(
                f"extracted Python does not parse: {exc}"
            ) from exc
        return code

    # --- Step 3 & 4 helpers ----------------------------------------------

    def parse_existing_tests(self, content: str) -> list:
        """Returns list[TestFunction] from utils.test_parser. The protocol
        types this as list[ExistingTest] for forward compatibility but the
        actual return type is the AST-level TestFunction shape.
        """
        return prompts.parse_existing_test_functions(content)

    def merge_new_tests(self, existing: str, new_tests: str) -> str:
        return prompts.merge_new_tests(existing, new_tests)

    def covers(self, test_name: str, source_function_name: str) -> bool:
        return prompts.covers(test_name, source_function_name)

    # --- Internal helpers for the incremental merge flow ----------------
    # These are not part of the LanguageHandler protocol — they're called
    # directly by the TestGenerator step. Once Java is added, we'll evaluate
    # whether to lift these into the protocol or leave them as Python-only.

    def extract_test_source(
        self, content: str, tests: list
    ) -> str:
        return prompts.extract_test_source(content, tests)

    def remove_tests(self, content: str, to_remove: list) -> str:
        return prompts.remove_tests(content, to_remove)
