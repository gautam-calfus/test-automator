"""Python test file path conventions. Moved from steps/test_finder.py
during the v0.2.0 plugin refactor.
"""

from __future__ import annotations

import os

_TEST_DIR_HINTS = ("/test_", "/tests/", "/test/")


def is_test_file(file_path: str) -> bool:
    """True for pytest-style test modules."""
    if not file_path.endswith(".py"):
        return False
    name = os.path.basename(file_path)
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    return any(hint in file_path for hint in _TEST_DIR_HINTS)


def candidate_test_paths(
    source_path: str, test_dirs: list[str]
) -> list[str]:
    """All paths the existing test file *might* be at, priority order.

    For src/calculator/tax.py with test_dirs=["tests"]:
      - tests/test_tax.py
      - src/calculator/test_tax.py
      - src/test_tax.py
    """
    stem = os.path.splitext(os.path.basename(source_path))[0]
    test_name = f"test_{stem}.py"
    candidates: list[str] = []

    for test_dir in test_dirs:
        candidates.append(os.path.join(test_dir, test_name))

    source_dir = os.path.dirname(source_path)
    candidates.append(os.path.join(source_dir, test_name))
    candidates.append(os.path.join(source_dir, "..", "tests", test_name))
    return candidates


def suggest_test_path(source_path: str, test_dirs: list[str]) -> str:
    """Canonical path for a new test file."""
    stem = os.path.splitext(os.path.basename(source_path))[0]
    test_name = f"test_{stem}.py"
    preferred_dir = test_dirs[0] if test_dirs else "tests"
    return os.path.join(preferred_dir, test_name)


def temp_test_file_name(test_file_path: str) -> str:
    """Name to give the temp file pytest discovers during the run.

    Prefixed with ``_pr_automator_`` so we can identify and clean up temp
    files; this prefix doesn't break pytest discovery the way a leading
    dot would.
    """
    base = os.path.basename(test_file_path)
    return f"_pr_automator_{base}"
