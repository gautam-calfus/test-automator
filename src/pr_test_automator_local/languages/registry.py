"""Registry mapping language names and file extensions to handlers.

By default, only Python is registered (in ``languages/__init__.py``). Future
languages register themselves the same way. Custom languages can be added by
calling ``register_language(my_handler)``.
"""

from __future__ import annotations

import os

from pr_test_automator_local.languages.base import LanguageHandler

_handlers_by_name: dict[str, LanguageHandler] = {}
_handlers_by_extension: dict[str, LanguageHandler] = {}


def register_language(handler: LanguageHandler) -> None:
    """Add a language handler to the registry.

    Raises ValueError if a handler with the same name is already registered
    (call ``unregister_language`` first if you intend to override).
    """
    if handler.name in _handlers_by_name:
        raise ValueError(
            f"Language '{handler.name}' is already registered. Call "
            f"unregister_language('{handler.name}') first if you intend "
            f"to override."
        )
    _handlers_by_name[handler.name] = handler
    for ext in handler.source_extensions:
        if ext in _handlers_by_extension:
            existing = _handlers_by_extension[ext].name
            raise ValueError(
                f"Extension '{ext}' is already claimed by language "
                f"'{existing}'. Two handlers cannot claim the same "
                f"extension."
            )
        _handlers_by_extension[ext] = handler


def unregister_language(name: str) -> None:
    """Remove a previously-registered handler. Useful for tests."""
    handler = _handlers_by_name.pop(name, None)
    if handler is None:
        return
    for ext in handler.source_extensions:
        _handlers_by_extension.pop(ext, None)


def get_handler_by_name(name: str) -> LanguageHandler:
    """Look up a handler by its short name (e.g. "python")."""
    if name not in _handlers_by_name:
        available = ", ".join(sorted(_handlers_by_name)) or "(none registered)"
        raise KeyError(
            f"No language handler registered for '{name}'. "
            f"Available: {available}"
        )
    return _handlers_by_name[name]


def get_handler_for_file(file_path: str) -> LanguageHandler | None:
    """Look up a handler by file extension. Returns None for unknown
    extensions so the caller can decide whether to skip or error.
    """
    ext = os.path.splitext(file_path)[1]
    return _handlers_by_extension.get(ext)


def all_source_extensions() -> tuple[str, ...]:
    """Every extension claimed by some registered handler.

    The diff reader uses this when no specific language is configured, so
    we can find files across all enabled languages.
    """
    return tuple(sorted(_handlers_by_extension))


def all_languages() -> tuple[str, ...]:
    """Names of all registered languages, for the ``--language`` CLI help."""
    return tuple(sorted(_handlers_by_name))
