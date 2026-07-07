"""Language plugin infrastructure for test-automator.

Each language is a plugin implementing ``LanguageHandler``. Python,
Kotlin, Java, and JavaScript/TypeScript are registered by default.
Future plugins register the same way.

Public API:
    LanguageHandler        — the protocol every plugin must implement
    register_language      — add a custom or third-party plugin
    unregister_language    — remove one (mostly useful in tests)
    get_handler_by_name    — fetch a registered plugin
    get_handler_for_file   — pick a plugin based on a file extension
    all_languages          — list registered plugin names
    all_source_extensions  — list all claimed extensions
"""

from test_automator.languages.base import LanguageHandler
from test_automator.languages.java import JavaLanguageHandler
from test_automator.languages.javascript import JavaScriptLanguageHandler
from test_automator.languages.kotlin import KotlinLanguageHandler
from test_automator.languages.python import PythonLanguageHandler
from test_automator.languages.registry import (
    all_languages,
    all_source_extensions,
    get_handler_by_name,
    get_handler_for_file,
    register_language,
    unregister_language,
)

# Register the built-in handlers so they're available out of the box.
register_language(PythonLanguageHandler())
register_language(KotlinLanguageHandler())
register_language(JavaLanguageHandler())
register_language(JavaScriptLanguageHandler())

__all__ = [
    "LanguageHandler",
    "PythonLanguageHandler",
    "KotlinLanguageHandler",
    "JavaLanguageHandler",
    "JavaScriptLanguageHandler",
    "register_language",
    "unregister_language",
    "get_handler_by_name",
    "get_handler_for_file",
    "all_languages",
    "all_source_extensions",
]
