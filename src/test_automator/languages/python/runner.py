"""Python pytest invocation and output parsing.

Moved from steps/test_runner.py during the v0.2.0 plugin refactor.
"""

from __future__ import annotations

import os
import shutil
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


def _uses_uv(repo_path: str) -> bool:
    """True when the project looks uv-managed.

    Two signals, either is enough: a ``uv.lock`` at the repo root (uv
    writes one on every ``uv add``/``uv sync``), or a ``[tool.uv]`` /
    ``[tool.uv.*]`` table in ``pyproject.toml``. We read the file as
    text rather than parsing TOML so we don't need a parser on 3.10 (no
    stdlib ``tomllib`` before 3.11) and don't add a dependency.
    """
    if os.path.isfile(os.path.join(repo_path, "uv.lock")):
        return True
    pyproject = os.path.join(repo_path, "pyproject.toml")
    try:
        with open(pyproject, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return False
    return "[tool.uv]" in text or "[tool.uv." in text


def _uv_available() -> bool:
    return shutil.which("uv") is not None


def _pytest_prefix(repo_path: str, python_runner: str = "auto") -> list[str]:
    """Choose how to invoke pytest for this project.

    ``python_runner`` is the user's preference:
    - ``"auto"`` (default): use ``uv run`` only when the project looks
      uv-managed AND the ``uv`` binary is installed — otherwise fall
      back to plain ``python -m pytest`` so existing pip/venv projects
      are unaffected.
    - ``"uv"``: always go through ``uv run``. If ``uv`` isn't installed
      the run fails with a clear "command not found" error rather than
      silently using a different interpreter — that's the point of an
      explicit override.
    - ``"pip"`` / ``"python"``: always plain ``python -m pytest``.

    ``uv run`` resolves and (if needed) syncs the project's environment
    before running, so tests execute against the exact deps in
    ``uv.lock`` instead of whatever happens to be on ``PATH``.
    """
    runner = (python_runner or "auto").lower()
    if runner == "auto":
        if _uses_uv(repo_path) and _uv_available():
            return ["uv", "run", "python", "-m", "pytest"]
        return ["python", "-m", "pytest"]
    if runner == "uv":
        return ["uv", "run", "python", "-m", "pytest"]
    return ["python", "-m", "pytest"]


def build_test_command(
    test_files: list[str], repo_path: str, python_runner: str = "auto"
) -> list[str]:
    """Return the argv list for invoking pytest.

    The invocation is prefixed with ``uv run`` for uv-managed projects
    (see ``_pytest_prefix``); otherwise it's the classic
    ``python -m pytest``.

    The ``-o addopts=`` override clears any project-level pytest config that
    would inject incompatible plugins (e.g. pytest-cov when not installed).
    ``no:cacheprovider`` skips pytest's cache, which has no value for these
    ephemeral runs.
    """
    return [
        *_pytest_prefix(repo_path, python_runner),
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
