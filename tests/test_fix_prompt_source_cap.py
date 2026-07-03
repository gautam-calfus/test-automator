"""Tests for v0.3.0a4: fix-mode source cap.

Real user scenario: on a run against Acme, a fix-loop attempt on
CMService.java (267K chars) produced a prompt with chars=278867 — one
LLM call consuming ~70K tokens. That single call likely burned more
quota than everything else in the run combined.

The fix: when the source is over 30K chars, use compact class-signature
extraction (imports + class declaration + method signatures, no bodies)
instead of the raw file. On CMService.java this cuts 267K → ~17K.
"""

from __future__ import annotations

import pytest

from pr_test_automator_local.languages.java.prompts import (
    _FIX_SOURCE_HARD_CAP,
    _read_source_capped,
    user_prompt_fix,
)
from pr_test_automator_local.models import GeneratedTest


def test_small_source_is_returned_as_is(tmp_path):
    """Files under the cap should be returned raw so Claude has full
    context — the cap only kicks in for oversized files.
    """
    source_path = tmp_path / "SmallClass.java"
    content = "package com.acme;\n\npublic class SmallClass {\n    void doWork() {}\n}\n"
    source_path.write_text(content)

    result = _read_source_capped(str(source_path))

    assert result == content
    assert len(result) < _FIX_SOURCE_HARD_CAP


def test_oversized_source_falls_back_to_signatures(tmp_path):
    """Files over the cap should be compressed to signatures. Verify
    the output is smaller than the input AND smaller than the cap.
    """
    source_path = tmp_path / "HugeClass.java"

    # Build a synthetic Java file just over the cap. Padding with
    # long method bodies since signature extraction will strip those.
    method_body = "\n".join(f"        int x{i} = {i};" for i in range(50))
    methods = "\n".join(
        f"    public void method{i}() {{\n{method_body}\n    }}\n"
        for i in range(60)
    )
    content = (
        f"package com.acme;\n\npublic class HugeClass {{\n{methods}\n}}\n"
    )
    source_path.write_text(content)

    assert len(content) > _FIX_SOURCE_HARD_CAP, "Test setup: not big enough"

    result = _read_source_capped(str(source_path))

    # Result should be smaller than raw AND smaller than cap
    assert len(result) < len(content)
    assert len(result) < _FIX_SOURCE_HARD_CAP
    # Should have the note explaining what was done
    assert "compact" in result.lower() or "signatures" in result.lower()
    # Should preserve the class declaration
    assert "class HugeClass" in result


def test_unreadable_source_returns_placeholder():
    """When the source file can't be read, we return a placeholder
    rather than crashing the fix loop.
    """
    result = _read_source_capped("/nonexistent/path/File.java")
    assert "unavailable" in result.lower()


def test_user_prompt_fix_bounds_total_prompt_size(tmp_path):
    """End-to-end: user_prompt_fix should never produce a prompt over
    a reasonable size, even on a giant source file. This is the
    scenario that produced chars=278867 in the wild.
    """
    # Build a 250K-char source file (comparable to CMService.java)
    source_path = tmp_path / "BigClass.java"
    method_body = "\n".join(f"        int x{i} = {i};" for i in range(100))
    methods = "\n".join(
        f"    public void method{i}() {{\n{method_body}\n    }}\n"
        for i in range(120)
    )
    content = (
        f"package com.acme;\n\npublic class BigClass {{\n{methods}\n}}\n"
    )
    source_path.write_text(content)
    assert len(content) > 200_000, "Test setup: not big enough"

    generated = GeneratedTest(
        source_file_path=str(source_path),
        test_file_path=str(tmp_path / "BigClassTest.java"),
        content="package com.acme;\nclass BigClassTest {}\n",
        covered_functions=[],
    )

    prompt = user_prompt_fix(generated, runner_output="some error output")

    # The final prompt should be dramatically smaller than the raw source.
    # Being generous: allow up to 80K chars (cap 30K + templates +
    # runner output up to 8K + test content). But it must NOT be
    # close to the 278K we saw in the wild.
    assert len(prompt) < 80_000, (
        f"Prompt is {len(prompt):,} chars — this is the exact bug "
        f"v0.3.0a4 was supposed to fix. Expected <80K."
    )
