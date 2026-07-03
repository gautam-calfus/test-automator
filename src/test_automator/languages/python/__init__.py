"""Python language plugin for test-automator.

Generates pytest tests using Python's built-in ast module for parsing.
"""

from test_automator.languages.python.handler import (
    PythonLanguageHandler,
)

__all__ = ["PythonLanguageHandler"]
