"""Tests for the language plugin registry and Python handler.

Added in v0.2.0a1 to verify the refactor maintains backward compatibility
with the original Python-only behavior.
"""

from __future__ import annotations

import pytest

from pr_test_automator_local.languages import (
    PythonLanguageHandler,
    all_languages,
    all_source_extensions,
    get_handler_by_name,
    get_handler_for_file,
    register_language,
    unregister_language,
)
from pr_test_automator_local.languages.base import LanguageHandler


# ---------------------------------------------------------------------------
# Registry behavior
# ---------------------------------------------------------------------------


def test_python_is_registered_by_default() -> None:
    assert "python" in all_languages()


def test_python_handler_resolved_by_name() -> None:
    handler = get_handler_by_name("python")
    assert handler.name == "python"
    assert ".py" in handler.source_extensions


def test_python_handler_resolved_by_extension() -> None:
    handler = get_handler_for_file("src/foo/bar.py")
    assert handler is not None
    assert handler.name == "python"


def test_unknown_extension_returns_none() -> None:
    # .klingon isn't claimed by any registered handler
    assert get_handler_for_file("src/foo/bar.klingon") is None


def test_all_source_extensions_includes_py() -> None:
    assert ".py" in all_source_extensions()


def test_unknown_name_raises() -> None:
    with pytest.raises(KeyError, match="No language handler"):
        get_handler_by_name("klingon")


def test_double_registration_rejected() -> None:
    second = PythonLanguageHandler()
    with pytest.raises(ValueError, match="already registered"):
        register_language(second)


def test_unregister_then_reregister_works() -> None:
    # Remove Python, confirm it's gone, then re-add via the same handler
    # to leave the registry in a sane state for other tests.
    unregister_language("python")
    try:
        assert "python" not in all_languages()
        assert get_handler_for_file("src/foo/bar.py") is None
    finally:
        register_language(PythonLanguageHandler())
    assert "python" in all_languages()


# ---------------------------------------------------------------------------
# Python handler — sanity checks of the protocol contract
# ---------------------------------------------------------------------------


def test_python_handler_implements_protocol() -> None:
    handler = PythonLanguageHandler()
    assert isinstance(handler, LanguageHandler)


def test_python_handler_extract_affected_finds_changed_function() -> None:
    handler = PythonLanguageHandler()
    source = (
        "def untouched():\n"
        "    return 1\n"
        "\n"
        "def changed(x):\n"
        "    return x + 1\n"
    )
    # Lines 4 (def) and 5 (body) of the changed function
    affected = handler.extract_affected(source, "src/foo.py", {4, 5})
    names = [fn.name for fn in affected]
    assert "changed" in names
    assert "untouched" not in names


def test_python_handler_suggest_test_path() -> None:
    handler = PythonLanguageHandler()
    handler.configure(["tests"])
    assert handler.suggest_test_path("src/foo/bar.py") == "tests/test_bar.py"


def test_python_handler_temp_file_naming() -> None:
    handler = PythonLanguageHandler()
    assert handler.temp_test_file_name(
        "tests/test_foo.py"
    ) == "_pr_automator_test_foo.py"


def test_python_handler_is_test_file_recognizes_pytest_names() -> None:
    handler = PythonLanguageHandler()
    assert handler.is_test_file("tests/test_foo.py")
    assert handler.is_test_file("src/foo/test_bar.py")
    assert handler.is_test_file("src/foo_test.py")
    assert not handler.is_test_file("src/foo/bar.py")


def test_python_handler_covers_exact_and_prefix() -> None:
    handler = PythonLanguageHandler()
    assert handler.covers("test_foo", "foo")
    assert handler.covers("test_foo_zero", "foo")
    assert not handler.covers("test_bar", "foo")


def test_python_handler_collection_error_markers_include_import_error() -> None:
    handler = PythonLanguageHandler()
    markers = handler.collection_error_markers()
    assert "ImportError" in markers
    assert "ModuleNotFoundError" in markers


def test_python_handler_build_test_command_uses_pytest() -> None:
    handler = PythonLanguageHandler()
    cmd = handler.build_test_command(
        ["tests/_pr_automator_test_foo.py"], "/repo"
    )
    assert cmd[:3] == ["python", "-m", "pytest"]
    assert "tests/_pr_automator_test_foo.py" in cmd


def test_python_handler_parse_test_output_passing() -> None:
    handler = PythonLanguageHandler()
    output = "== 5 passed in 0.12s =="
    result = handler.parse_test_output(output, 0)
    assert result["passed"] == 5
    assert result["failed"] == 0
    assert result["is_passing"] is True


def test_python_handler_parse_test_output_failing() -> None:
    handler = PythonLanguageHandler()
    output = "FAILED tests/test_foo.py::test_x\n== 1 failed, 2 passed in 0.1s =="
    result = handler.parse_test_output(output, 1)
    assert result["passed"] == 2
    assert result["failed"] == 1
    assert result["is_passing"] is False
    assert "tests/test_foo.py::test_x" in result["failed_test_ids"]


def test_python_handler_parse_test_output_import_error() -> None:
    handler = PythonLanguageHandler()
    output = "ImportError while importing test module"
    result = handler.parse_test_output(output, 1)
    assert result["errors"] == 1
    assert result["is_passing"] is False


def test_python_handler_merge_new_tests_preserves_existing_then_appends() -> None:
    """Verify the existing content is preserved and new tests are appended.

    The exact spacing inside the new-tests section is determined by the
    regex in prompts._collapse_blank_runs; this test only checks the
    boundary properties: existing is preserved, new content appears after.
    """
    handler = PythonLanguageHandler()
    existing = "import pytest\n\n\ndef test_a():\n    assert True\n"
    new_tests = (
        "@pytest.mark.unit\ndef test_b():\n    assert True\n\n\n"
        "@pytest.mark.unit\ndef test_c():\n    assert True\n"
    )
    merged = handler.merge_new_tests(existing, new_tests)
    # The existing function survives intact at the start
    assert merged.startswith("import pytest\n\n\ndef test_a():\n    assert True")
    # The new functions all appear in the merged output
    assert "def test_b" in merged
    assert "def test_c" in merged
    # Existing content is separated from new content by blank lines
    assert "test_a():\n    assert True\n\n" in merged


# ---------------------------------------------------------------------------
# Diff reader integration
# ---------------------------------------------------------------------------


def test_diff_reader_skips_non_python_extensions(tmp_path) -> None:
    """Files for languages without a registered handler should be skipped.

    With Java now registered (Stage 2), .java files DO get picked up — see
    test_diff_reader_now_includes_java_files in test_java.py. This test
    only checks that totally-unknown extensions are still excluded.
    """
    from pr_test_automator_local.config import LocalTestConfig
    from pr_test_automator_local.steps.local_diff_reader import (
        LocalDiffReader,
    )

    reader = LocalDiffReader(LocalTestConfig(repo_path=str(tmp_path)))
    # Truly unknown extensions still excluded
    assert not reader._is_eligible_source("src/foo.klingon")
    assert not reader._is_eligible_source("README.md")
    # Python source file is eligible
    assert reader._is_eligible_source("src/foo.py")
    # Python test file is excluded
    assert not reader._is_eligible_source("tests/test_foo.py")
