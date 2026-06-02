"""Pydantic models shared across pipeline steps."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class PRFile(BaseModel):
    """A single file changed since the base branch."""

    filename: str
    status: str  # added | modified | removed
    patch: str | None = None


class PRInfo(BaseModel):
    """Metadata about the current local change set."""

    number: int  # Always 0 for local runs
    title: str
    head_branch: str
    base_branch: str
    author: str
    files: list[PRFile]


class AffectedFunction(BaseModel):
    """A Python function/class whose lines overlap with the diff."""

    file_path: str
    name: str
    qualified_name: str
    kind: str
    source_code: str
    line_start: int
    line_end: int


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
