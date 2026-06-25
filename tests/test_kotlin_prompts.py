"""Tests for Kotlin LLM prompts and content transformations (Stage 4a)."""

from __future__ import annotations

import pytest

from pr_test_automator_local.languages.kotlin import prompts
from pr_test_automator_local.languages.kotlin.handler import (
    KotlinLanguageHandler,
)
from pr_test_automator_local.models import AffectedFunction


# ---------------------------------------------------------------------------
# Path / package / class-name derivation
# ---------------------------------------------------------------------------


def test_derive_source_package_from_initech_qualified_name() -> None:
    aff = AffectedFunction(
        file_path="src/main/kotlin/com/initech/accounts/services/Foo.kt",
        name="bar",
        qualified_name="com.initech.accounts.services.Foo.bar",
        kind="method",
        source_code="fun bar() {}",
        line_start=1,
        line_end=1,
    )
    assert prompts._derive_source_package([aff]) == (
        "com.initech.accounts.services"
    )


def test_derive_source_package_for_nested_class() -> None:
    """For methods inside nested classes, the package excludes both the
    outer and inner class names.
    """
    aff = AffectedFunction(
        file_path="src/main/kotlin/com/initech/accounts/services/Foo.kt",
        name="bar",
        qualified_name="com.initech.accounts.services.Foo.Inner.bar",
        kind="method",
        source_code="fun bar() {}",
        line_start=1,
        line_end=1,
    )
    assert prompts._derive_source_package([aff]) == (
        "com.initech.accounts.services"
    )


def test_derive_test_package_for_initech() -> None:
    assert prompts._derive_test_package(
        "com.initech.accounts.services"
    ) == "unit.services"


def test_derive_test_package_for_subpath() -> None:
    assert prompts._derive_test_package(
        "com.initech.accounts.services.salesforce"
    ) == "unit.services.salesforce"


def test_derive_test_package_for_non_initech_fallback() -> None:
    assert prompts._derive_test_package("com.example.foo") == "unit.foo"


def test_derive_test_file_path_canonical() -> None:
    """Test file path is canonical (no _PRBot prefix) — temp naming
    happens in the runner, not in the path that gets stored on
    GeneratedTest.
    """
    path = prompts._derive_test_file_path(
        "src/main/kotlin/com/initech/accounts/services/salesforce/SalesforceService.kt"
    )
    assert path == (
        "src/test/kotlin/unit/services/salesforce/SalesforceServiceTests.kt"
    )


def test_derive_test_class_name_canonical() -> None:
    name = prompts._derive_test_class_name(
        "src/main/kotlin/com/initech/accounts/services/SalesforceService.kt"
    )
    # NO _PRBot prefix — the LLM generates with the canonical name
    assert name == "SalesforceServiceTests"


# ---------------------------------------------------------------------------
# User prompt construction
# ---------------------------------------------------------------------------


def test_user_prompt_fresh_includes_required_metadata() -> None:
    aff = AffectedFunction(
        file_path="src/main/kotlin/com/initech/accounts/services/Foo.kt",
        name="bar",
        qualified_name="com.initech.accounts.services.Foo.bar",
        kind="method",
        source_code="fun bar(): Int = 42",
        line_start=1,
        line_end=1,
    )
    prompt = prompts.user_prompt_fresh(
        "src/main/kotlin/com/initech/accounts/services/Foo.kt", [aff]
    )
    # Source location
    assert "src/main/kotlin/com/initech/accounts/services/Foo.kt" in prompt
    # Source package
    assert "com.initech.accounts.services" in prompt
    # Canonical test path (NOT _PRBot)
    assert "src/test/kotlin/unit/services/FooTests.kt" in prompt
    assert "_PRBot" not in prompt  # User prompt mentions only canonical
    # Test class name (canonical)
    assert "FooTests" in prompt
    # Test package
    assert "unit.services" in prompt
    # The actual source code to test
    assert "fun bar(): Int = 42" in prompt


def test_user_prompt_fresh_concatenates_multiple_functions() -> None:
    affected = [
        AffectedFunction(
            file_path="src/main/kotlin/com/initech/accounts/services/Foo.kt",
            name="bar",
            qualified_name="com.initech.accounts.services.Foo.bar",
            kind="method",
            source_code="fun bar(): Int = 1",
            line_start=1,
            line_end=1,
        ),
        AffectedFunction(
            file_path="src/main/kotlin/com/initech/accounts/services/Foo.kt",
            name="baz",
            qualified_name="com.initech.accounts.services.Foo.baz",
            kind="method",
            source_code="fun baz(): Int = 2",
            line_start=2,
            line_end=2,
        ),
    ]
    prompt = prompts.user_prompt_fresh(
        "src/main/kotlin/com/initech/accounts/services/Foo.kt", affected
    )
    assert "fun bar(): Int = 1" in prompt
    assert "fun baz(): Int = 2" in prompt


# ---------------------------------------------------------------------------
# Class rename: canonical → temp form
# ---------------------------------------------------------------------------


def test_rename_class_to_temp_form_basic() -> None:
    content = (
        "package unit.services\n"
        "\n"
        "class JwtAuthServiceTests {\n"
        "    @Test fun `test`() {}\n"
        "}\n"
    )
    result = prompts.rename_class_to_temp_form(
        content, "JwtAuthServiceTests"
    )
    assert "class _PRBotJwtAuthServiceTests {" in result
    assert "class JwtAuthServiceTests {" not in result


def test_rename_class_to_temp_form_preserves_imports() -> None:
    """The rename must not touch import lines or strings that contain
    the class name.
    """
    content = (
        "import com.initech.test.JwtAuthServiceTests\n"
        "\n"
        "class JwtAuthServiceTests {\n"
        "    val msg = \"JwtAuthServiceTests is great\"\n"
        "}\n"
    )
    result = prompts.rename_class_to_temp_form(
        content, "JwtAuthServiceTests"
    )
    # Class declaration was renamed
    assert "class _PRBotJwtAuthServiceTests" in result
    # Import preserved
    assert "import com.initech.test.JwtAuthServiceTests" in result
    # String literal preserved
    assert '"JwtAuthServiceTests is great"' in result


def test_rename_class_to_temp_form_only_replaces_class_keyword() -> None:
    """The regex requires the ``class`` keyword to precede the name —
    so a function or property called ``JwtAuthServiceTests`` would not
    be incorrectly renamed.
    """
    content = (
        "val classJwtAuthServiceTests = 42\n"
        "fun JwtAuthServiceTests() {}\n"
        "class JwtAuthServiceTests {}\n"
    )
    result = prompts.rename_class_to_temp_form(
        content, "JwtAuthServiceTests"
    )
    # Only the ``class`` declaration line is renamed
    assert "val classJwtAuthServiceTests = 42" in result
    assert "fun JwtAuthServiceTests()" in result
    assert "class _PRBotJwtAuthServiceTests {}" in result


# ---------------------------------------------------------------------------
# Handler integration
# ---------------------------------------------------------------------------


def test_handler_system_prompt_fresh_exists() -> None:
    h = KotlinLanguageHandler()
    prompt = h.system_prompt_fresh()
    assert "Kotlin" in prompt
    assert "MockK" in prompt
    assert "Strikt" in prompt
    assert "backticked" in prompt.lower()


def test_handler_user_prompt_fresh_works() -> None:
    aff = AffectedFunction(
        file_path="src/main/kotlin/com/initech/accounts/services/Foo.kt",
        name="bar",
        qualified_name="com.initech.accounts.services.Foo.bar",
        kind="method",
        source_code="fun bar() = 42",
        line_start=1,
        line_end=1,
    )
    h = KotlinLanguageHandler()
    prompt = h.user_prompt_fresh(
        "src/main/kotlin/com/initech/accounts/services/Foo.kt", [aff]
    )
    assert "Foo.kt" in prompt
    assert "fun bar() = 42" in prompt


def test_handler_incremental_prompt_now_succeeds() -> None:
    """In Stage 4b, the incremental system prompt returns a non-empty
    string (previously raised NotImplementedError in v0.2.0a5).
    """
    h = KotlinLanguageHandler()
    p = h.system_prompt_incremental()
    assert isinstance(p, str) and len(p) > 100
    assert "MockK" in p
    assert "Strikt" in p


def test_handler_fix_prompt_now_succeeds() -> None:
    """In Stage 4b, the fix system prompt returns a non-empty string
    (previously raised NotImplementedError in v0.2.0a5).
    """
    h = KotlinLanguageHandler()
    p = h.system_prompt_fix()
    assert isinstance(p, str) and len(p) > 100
    assert "Gradle" in p or "fail" in p.lower()


def test_handler_transform_for_temp_file_renames_class() -> None:
    """The runner calls this before writing the temp file. It must
    rename the canonical class name to the ``_PRBot`` form.
    """
    h = KotlinLanguageHandler()
    content = (
        "package unit.services\n"
        "class FooTests {\n"
        "    @Test fun `foo`() {}\n"
        "}\n"
    )
    transformed = h.transform_for_temp_file(
        content, "src/test/kotlin/unit/services/FooTests.kt"
    )
    assert "class _PRBotFooTests {" in transformed
    assert "class FooTests {" not in transformed


def test_handler_does_not_define_transform_for_commit() -> None:
    """transform_for_commit was REMOVED. The original content (with the
    canonical class name) is what gets committed — no transformation
    needed. If a transform_for_commit reappears, this test will catch
    it.
    """
    h = KotlinLanguageHandler()
    assert not hasattr(h, "transform_for_commit")


# ---------------------------------------------------------------------------
# Roundtrip: simulated end-to-end content flow
# ---------------------------------------------------------------------------


def test_end_to_end_content_flow() -> None:
    """Simulates the lifecycle of Kotlin test content from LLM output
    through temp-file run to committed content.

    1. LLM generates content with canonical class name
    2. transform_for_temp_file renames to _PRBot form (for runner)
    3. After test passes, committer writes ORIGINAL content (with
       canonical name) — no further transformation
    """
    # Step 1: LLM output (canonical class)
    llm_output = (
        "package unit.services\n"
        "import org.junit.jupiter.api.Test\n"
        "import strikt.api.expectThat\n"
        "import strikt.assertions.*\n"
        "\n"
        "class CalculatorTests {\n"
        "    @Test fun `add returns sum`() {\n"
        "        expectThat(1 + 1).isEqualTo(2)\n"
        "    }\n"
        "}\n"
    )

    h = KotlinLanguageHandler()

    # Step 2: runner transforms for temp file
    temp_content = h.transform_for_temp_file(
        llm_output, "src/test/kotlin/unit/services/CalculatorTests.kt"
    )
    assert "class _PRBotCalculatorTests {" in temp_content

    # Step 3: committer writes the original (no transform)
    # The committer writes ``gen.content`` directly, which is unchanged
    # from the LLM output. So the canonical file contains the canonical
    # class name.
    committed_content = llm_output  # this is what the committer writes
    assert "class CalculatorTests {" in committed_content
    assert "_PRBot" not in committed_content
