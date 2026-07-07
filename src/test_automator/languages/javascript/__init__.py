"""JavaScript/TypeScript (Node.js) language plugin.

Covers plain JavaScript, TypeScript, and their JSX/TSX variants, with
Jest as the primary test framework (Vitest is auto-detected and used
when the project depends on it — its CLI and JSON output are
Jest-compatible).
"""

from test_automator.languages.javascript.handler import (
    JavaScriptLanguageHandler,
)

__all__ = ["JavaScriptLanguageHandler"]
