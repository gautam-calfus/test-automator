"""Python pytest invocation and output parsing.

Moved from steps/test_runner.py during the v0.2.0 plugin refactor.
"""

from __future__ import annotations

import re

_SUMMARY_RE = re.compile(
    r"(?P<count>\d+)\s+(?P<kind>passed|failed|error|errors)",
    re.IGNORECASE,
)
_FAILED_ID_RE = re.compile(r"FAILED\s+(\S+)")

_COLLECTION_ERROR_MARKERS = (
    "ImportError",
    "ModuleNotFoundError",
    "no tests ran",
    "errors during collection",
)


def build_test_command(test_files: list[str], repo_path: str) -> list[str]:
    """Return the argv list for invoking pytest.

    The ``-o addopts=`` override clears any project-level pytest config that
    would inject incompatible plugins (e.g. pytest-cov when not installed).
    ``no:cacheprovider`` skips pytest's cache, which has no value for these
    ephemeral runs.
    """
    return [
        "python",
        "-m",
        "pytest",
        "--tb=short",
        "--no-header",
        "-v",
        "-o",
        "addopts=",
        "-p",
        "no:cacheprovider",
        *test_files,
    ]


def parse_test_output(
    output: str, return_code: int
) -> dict[str, int | bool | list[str]]:
    """Convert pytest output into structured counts.

    Returns a dict with passed/failed/errors counts, failed_test_ids list,
    and is_passing bool. The orchestrator turns this into a TestRunResult.
    """
    passed = failed = errors = 0
    for match in _SUMMARY_RE.finditer(output):
        count = int(match.group("count"))
        kind = match.group("kind").lower()
        if kind == "passed":
            passed = count
        elif kind == "failed":
            failed = count
        elif kind in {"error", "errors"}:
            errors = count

    failed_ids = _FAILED_ID_RE.findall(output)

    total_explicit = passed + failed + errors
    no_summary = total_explicit == 0
    has_collection_error = any(
        marker in output for marker in _COLLECTION_ERROR_MARKERS
    ) or "no tests ran" in output.lower()

    if no_summary and (has_collection_error or return_code != 0):
        errors = 1

    is_passing = return_code == 0 and not (no_summary and has_collection_error)

    return {
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "failed_test_ids": failed_ids,
        "is_passing": is_passing,
    }


def collection_error_markers() -> tuple[str, ...]:
    """Substrings that indicate test collection failed (vs assertion fail).

    Used by the fix loop to bail early — Claude can't fix an import error in
    the test file because the issue is in the user's project setup.
    """
    return _COLLECTION_ERROR_MARKERS
