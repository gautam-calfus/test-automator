"""Tests for v0.2.0a6.post4 changes:

- Change 1: Class signature extraction → fewer hallucinated APIs
- Change 2: Named args instruction in system prompts
- Change 3: Fix loop attempts compile errors instead of bailing
"""

from __future__ import annotations

import pytest

from pr_test_automator_local.languages.kotlin import analyzer, prompts, runner
from pr_test_automator_local.languages.kotlin.handler import (
    KotlinLanguageHandler,
)
from pr_test_automator_local.models import AffectedFunction


# ---------------------------------------------------------------------------
# Change 1: Class signature extraction
# ---------------------------------------------------------------------------


def test_extract_class_signatures_finds_data_class() -> None:
    """The actual ReferenceCodesSetting case that hit the post3 bug."""
    src = '''package x

data class ReferenceCode(
    val label: String,
    val values: List<String>,
    val required: Boolean,
    val freeText: Boolean? = false
) {
    fun assertValid() {
        require(label.isNotBlank())
    }
}
'''
    result = analyzer.extract_class_signatures(src)
    # Contains the data class declaration
    assert "data class ReferenceCode" in result
    # All 4 parameters present with their types
    assert "val label: String" in result
    assert "val values: List<String>" in result
    assert "val required: Boolean" in result
    assert "val freeText: Boolean? = false" in result
    # Method bodies are NOT included
    assert "require(label.isNotBlank())" not in result


def test_extract_class_signatures_finds_regular_class() -> None:
    """The SalesforceService case that hit the post3 bug."""
    src = """package x

class SalesforceService(
    private val config: SalesforceConfig,
    private val authenticator: SalesforceAuthenticator,
) {
    fun ping(): Boolean = isConfigured()
    private fun isConfigured(): Boolean = TODO()
}
"""
    result = analyzer.extract_class_signatures(src)
    assert "class SalesforceService" in result
    assert "private val config: SalesforceConfig" in result
    assert "private val authenticator: SalesforceAuthenticator" in result
    # Method bodies NOT included
    assert "ping(): Boolean = isConfigured()" not in result
    assert "private fun isConfigured" not in result


def test_extract_class_signatures_finds_multiple_classes() -> None:
    """A file with multiple top-level classes — all should be returned."""
    src = """package x

data class A(val x: Int)

class B(val name: String)
"""
    result = analyzer.extract_class_signatures(src)
    assert "data class A" in result
    assert "class B" in result


def test_extract_class_signatures_handles_empty_input() -> None:
    """Defensive: empty input returns empty string, no crash."""
    assert analyzer.extract_class_signatures("") == ""
    assert analyzer.extract_class_signatures("   \n  \n") == ""


def test_extract_class_signatures_handles_file_with_no_classes() -> None:
    """A file with only top-level functions returns empty."""
    src = """package x

fun foo() = 1
fun bar(x: Int): String = x.toString()
"""
    assert analyzer.extract_class_signatures(src) == ""


def test_extract_class_signatures_includes_annotation() -> None:
    """Class-level annotations like @JsonIgnoreProperties should be
    included so Claude sees the full declaration shape.
    """
    src = """package x

@JsonIgnoreProperties(ignoreUnknown = true)
data class ReferenceCode(
    val label: String,
)
"""
    result = analyzer.extract_class_signatures(src)
    assert "@JsonIgnoreProperties" in result
    assert "data class ReferenceCode" in result


def test_kotlin_handler_exposes_extract_class_signatures() -> None:
    """Sanity: the Kotlin handler exposes the new method so the
    code_analyzer can find it.
    """
    h = KotlinLanguageHandler()
    assert hasattr(h, "extract_class_signatures")
    src = "package x\nclass Foo(val name: String)\n"
    result = h.extract_class_signatures(src)
    assert "class Foo" in result


# ---------------------------------------------------------------------------
# Change 1: AffectedFunction carries class_context
# ---------------------------------------------------------------------------


def test_affected_function_has_class_context_field() -> None:
    """Backwards compatible: class_context defaults to empty string."""
    fn = AffectedFunction(
        file_path="x.kt",
        name="foo",
        qualified_name="x.foo",
        kind="function",
        source_code="fun foo() = 1",
        line_start=1,
        line_end=1,
    )
    assert fn.class_context == ""


def test_affected_function_carries_class_context() -> None:
    fn = AffectedFunction(
        file_path="x.kt",
        name="foo",
        qualified_name="x.foo",
        kind="function",
        source_code="fun foo() = 1",
        line_start=1,
        line_end=1,
        class_context="data class Foo(val x: Int)",
    )
    assert fn.class_context == "data class Foo(val x: Int)"


# ---------------------------------------------------------------------------
# Change 1: Class context appears in the prompts
# ---------------------------------------------------------------------------


def test_fresh_prompt_includes_class_signatures_section() -> None:
    fn = AffectedFunction(
        file_path="src/main/kotlin/com/initech/accounts/x/Foo.kt",
        name="foo",
        qualified_name="com.initech.accounts.x.foo",
        kind="function",
        source_code="fun foo() = 1",
        line_start=1,
        line_end=1,
        class_context=(
            "data class ReferenceCode(\n"
            "    val label: String,\n"
            "    val freeText: Boolean? = false,\n"
            ")"
        ),
    )
    prompt = prompts.user_prompt_fresh(fn.file_path, [fn])
    assert "CLASS SIGNATURES" in prompt
    assert "data class ReferenceCode" in prompt
    assert "freeText: Boolean? = false" in prompt
    # And the "use these exactly" instruction
    assert "USE THESE EXACTLY" in prompt or \
        "do not invent parameter names" in prompt.lower()


def test_fresh_prompt_handles_empty_class_context() -> None:
    """If class_context is empty (handler doesn't support it, or
    parsing failed), the prompt still builds without crashing.
    """
    fn = AffectedFunction(
        file_path="x.kt",
        name="foo",
        qualified_name="com.foo.bar.foo",
        kind="function",
        source_code="fun foo() = 1",
        line_start=1,
        line_end=1,
        class_context="",
    )
    prompt = prompts.user_prompt_fresh("src/main/kotlin/com/foo/bar/X.kt", [fn])
    # Should not crash. Should have a clear placeholder.
    assert "class signatures unavailable" in prompt.lower() or \
        "(no class signatures available)" in prompt


def test_incremental_prompt_includes_class_signatures_section() -> None:
    from pr_test_automator_local.models import ExistingTest

    fn = AffectedFunction(
        file_path="src/main/kotlin/com/initech/accounts/x/Foo.kt",
        name="foo",
        qualified_name="com.initech.accounts.x.foo",
        kind="function",
        source_code="fun foo() = 1",
        line_start=1,
        line_end=1,
        class_context="data class Foo(val x: Int)",
    )
    existing = ExistingTest(
        test_file_path="src/test/kotlin/unit/x/FooTests.kt",
        source_file_path=fn.file_path,
        content="class FooTests {\n}",
    )
    prompt = prompts.user_prompt_incremental(
        fn.file_path, existing, [fn], "class FooTests {\n}", "",
    )
    assert "CLASS SIGNATURES" in prompt
    assert "data class Foo(val x: Int)" in prompt


# ---------------------------------------------------------------------------
# Change 2: Named arguments instruction
# ---------------------------------------------------------------------------


def test_fresh_system_prompt_mentions_named_arguments() -> None:
    """The system prompt instructs Claude to use named args when calling
    data class constructors. This is the v0.2.0a6.post4 fix for the
    positional null bug.
    """
    prompt = prompts.SYSTEM_PROMPT_FRESH
    # Some form of "use named arguments" instruction
    assert "named arguments" in prompt.lower() or \
        "NAMED PARAMETERS" in prompt
    # The concrete example
    assert "ReferenceCode" in prompt or "freeText" in prompt or \
        "positional" in prompt.lower()


def test_incremental_system_prompt_mentions_named_arguments() -> None:
    """Same for incremental — the bug can happen in either mode."""
    prompt = prompts.SYSTEM_PROMPT_INCREMENTAL
    assert "named arguments" in prompt.lower() or \
        "NAMED PARAMETERS" in prompt


# ---------------------------------------------------------------------------
# Change 3: Fix loop attempts compile errors
# ---------------------------------------------------------------------------


def test_collection_markers_no_longer_include_compile_errors() -> None:
    """The whole point of post4: compile errors flow to the fix loop."""
    markers = runner.collection_error_markers()
    assert "Task :compileTestKotlin FAILED" not in markers
    assert "Task :compileKotlin FAILED" not in markers
    assert "Compilation error" not in markers


def test_collection_markers_still_include_environment_errors() -> None:
    """True environment errors (no internet, missing deps) still bail."""
    markers = runner.collection_error_markers()
    assert "Could not resolve all files for configuration" in markers
    assert "Could not start your build" in markers


def test_compile_error_no_longer_triggers_collection_bailout() -> None:
    """A real compile-error fixture should NOT match any collection
    error markers in post4 — the fix loop should engage on it.
    """
    compile_error_output = """
> Task :compileTestKotlin
e: /path/to/Test.kt: (108, 85): Null can not be a value of a non-null type Boolean
> Task :compileTestKotlin FAILED
FAILURE: Build failed with an exception.
* What went wrong:
Execution failed for task ':compileTestKotlin'.
> Compilation error. See log for more details
BUILD FAILED in 54s
"""
    markers = runner.collection_error_markers()
    triggered = [m for m in markers if m in compile_error_output]
    assert triggered == [], (
        f"Compile error fixture should NOT trigger collection bailout "
        f"in post4. Triggered: {triggered}"
    )


def test_environment_error_still_triggers_collection_bailout() -> None:
    """True environment errors should still trigger the bailout."""
    env_error_output = """
FAILURE: Build failed with an exception.
* What went wrong:
Could not resolve all files for configuration ':classpath'.
> Could not get resource 'https://repo.maven.apache.org/maven2/...'
"""
    markers = runner.collection_error_markers()
    triggered = [m for m in markers if m in env_error_output]
    assert len(triggered) > 0, (
        "Environment errors should still trigger collection bailout"
    )
