"""Tests for v0.3.0a2: per-file LLM failures are non-fatal.

Real user scenario: mid-batch, Claude Code hit its session quota on
file 12 of 25. Before this fix, one failed file crashed the whole
pipeline and lost ~40 minutes of successful generation work for files
1-11. After the fix, successful files are preserved and written to
disk; the failed file is logged and skipped.
"""

from __future__ import annotations

from unittest.mock import Mock, MagicMock

import pytest

from pr_test_automator_local.models import AffectedFunction, GeneratedTest
from pr_test_automator_local.steps.test_generator import TestGenerator
from pr_test_automator_local.utils.exceptions import TestGeneratorError


def _make_affected(source_path: str) -> AffectedFunction:
    """Build a minimal AffectedFunction for testing."""
    return AffectedFunction(
        file_path=source_path,
        name="someMethod",
        qualified_name=f"com.example.{source_path}.someMethod",
        kind="method_declaration",
        source_code="void someMethod() {}",
        line_start=1,
        line_end=1,
        diff_hunk="+ x",
        class_context="",
    )


def test_partial_llm_failure_preserves_successes(monkeypatch):
    """When one file's LLM call fails mid-batch, the successes from
    earlier files must be preserved and returned.
    """
    from pr_test_automator_local.config import LocalTestConfig

    config = LocalTestConfig(
        repo_path="/tmp", base_branch="main", source_root="src/main/java",
    )

    # Stub LLM: succeed on files 1-2, fail on file 3, succeed on file 4
    llm = Mock()
    call_count = {"n": 0}

    def _fake_llm(system, user):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise Exception("You've hit your session limit")
        return "package com.example;\n\nclass FooTest { @Test void x() {} }\n"

    llm.generate = _fake_llm

    # Stub handler with just enough surface area
    handler = MagicMock()
    handler.name = "java"
    handler.system_prompt_fresh.return_value = "system"
    handler.user_prompt_fresh.return_value = "user"
    handler.extract_code.return_value = "package com.example;\n\nclass FooTest { }\n"
    handler.suggest_test_path.side_effect = lambda src: src.replace(
        "/main/", "/test/"
    ).replace(".java", "Test.java")

    # Patch get_handler_for_file to return our stub
    monkeypatch.setattr(
        "pr_test_automator_local.steps.test_generator.get_handler_for_file",
        lambda _: handler,
    )

    # Build 4 affected functions in different files
    affected = [
        _make_affected(f"src/main/java/File{i}.java") for i in range(1, 5)
    ]

    # Test finder stub — must return real strings for pydantic
    test_finder = Mock()
    def _make_path(src, existing=None):
        return src.replace("/main/", "/test/").replace(".java", "Test.java")
    test_finder.suggest_test_path = _make_path

    generator = TestGenerator(config, test_finder, llm)
    results = generator.generate(affected, existing_tests=[])

    # File 3 failed, so we should have 3 results (files 1, 2, 4)
    assert len(results) == 3, (
        f"Expected 3 successful results (files 1, 2, 4 - file 3 failed), "
        f"got {len(results)}: {[r.source_file_path for r in results]}"
    )
    successful_paths = [r.source_file_path for r in results]
    assert "src/main/java/File3.java" not in successful_paths
    assert "src/main/java/File1.java" in successful_paths
    assert "src/main/java/File2.java" in successful_paths
    assert "src/main/java/File4.java" in successful_paths


def test_all_llm_failures_still_raise(monkeypatch):
    """If EVERY file's LLM call fails, we do raise — there's nothing to save."""
    from pr_test_automator_local.config import LocalTestConfig

    config = LocalTestConfig(
        repo_path="/tmp", base_branch="main", source_root="src/main/java",
    )

    # Every LLM call fails
    llm = Mock()
    llm.generate.side_effect = Exception("session limit")

    handler = MagicMock()
    handler.name = "java"
    handler.system_prompt_fresh.return_value = "system"
    handler.user_prompt_fresh.return_value = "user"

    monkeypatch.setattr(
        "pr_test_automator_local.steps.test_generator.get_handler_for_file",
        lambda _: handler,
    )

    affected = [_make_affected(f"src/main/java/File{i}.java") for i in range(1, 4)]

    test_finder = Mock()
    generator = TestGenerator(config, test_finder, llm)

    with pytest.raises(TestGeneratorError) as exc_info:
        generator.generate(affected, existing_tests=[])

    # Error message should mention the total count of failures
    assert "3" in str(exc_info.value) or "session limit" in str(exc_info.value)
