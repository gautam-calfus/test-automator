"""Pydantic models shared across pipeline steps."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class PRFile(BaseModel):
    """A single file changed since the base branch.

    ``base_content`` is the file's content at the merge-base with the
    base branch (None for files that didn't exist there). It lets the
    analyzer diff the base version's function list against the current
    one to detect REMOVED functions, so stale tests covering them can
    be pruned.
    """

    filename: str
    status: str  # added | modified | removed
    patch: str | None = None
    base_content: str | None = None


class PRInfo(BaseModel):
    """Metadata about the current local change set."""

    number: int  # Always 0 for local runs
    title: str
    head_branch: str
    base_branch: str
    author: str
    files: list[PRFile]


class AffectedFunction(BaseModel):
    """A function/class whose lines overlap with the diff.

    ``source_code`` is the full body of the function (or class). For a
    60-line function where the user changed 2 lines, ``source_code`` is
    all 60 lines — useful for Claude to understand the function's
    purpose.

    ``diff_hunk`` is just the changed lines (the ``+``/``-`` lines from
    the diff) that fall within this function. Used by the test
    prompts to tell Claude what specifically changed, so generated
    tests focus on the changes rather than re-testing the whole
    function exhaustively.

    Defaults to empty string for backwards compatibility (Python
    handler doesn't populate it yet).
    """

    file_path: str
    name: str
    qualified_name: str
    kind: str
    source_code: str
    line_start: int
    line_end: int
    diff_hunk: str = ""
    class_context: str = ""


class RemovedFunction(BaseModel):
    """A function/method that existed at the merge-base but is gone from
    the current source (deleted or renamed). Existing tests covering it
    are stale by definition — they reference symbols that no longer
    compile/resolve — and get pruned mechanically (no LLM call).
    """

    file_path: str
    name: str
    kind: str = "function"


class ExistingTest(BaseModel):
    test_file_path: str
    source_file_path: str
    content: str


class GeneratedTest(BaseModel):
    source_file_path: str
    test_file_path: str
    content: str
    covered_functions: list[str]


class TestRunResult(BaseModel):
    passed: int
    failed: int
    errors: int
    total: int
    output: str
    failed_test_ids: list[str]
    is_passing: bool


class StepOutcome(BaseModel):
    step: str
    success: bool
    message: str
    data: dict[str, Any] = {}


class PipelineResult(BaseModel):
    repo_path: str
    base_branch: str
    head_branch: str
    files_changed: int
    functions_affected: int
    tests_generated: int
    test_result: TestRunResult | None = None
    commit_sha: str | None = None
    pr_url: str | None = None
    steps: list[StepOutcome] = []
    is_passing: bool = False
