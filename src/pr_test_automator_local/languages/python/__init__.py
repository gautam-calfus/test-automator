"""Python language plugin for pr-test-automator-local.

Generates pytest tests using Python's built-in ast module for parsing.
"""

from pr_test_automator_local.languages.python.handler import (
    PythonLanguageHandler,
)

__all__ = ["PythonLanguageHandler"]
