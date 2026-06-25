"""Tests for the Kotlin language plugin (Stage 2).

These tests use inline Kotlin source mirroring Initech accounts-service
style (Strikt + MockK + backticked test names) rather than reading from
the actual project, so they're hermetic and run in CI without external
dependencies.
"""

from __future__ import annotations

import pytest

from pr_test_automator_local.languages import (
    KotlinLanguageHandler,
    all_languages,
    get_handler_by_name,
    get_handler_for_file,
)
from pr_test_automator_local.languages.base import LanguageHandler
from pr_test_automator_local.languages.kotlin import analyzer


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_kotlin_is_registered() -> None:
    assert "kotlin" in all_languages()


def test_kotlin_resolved_by_extension() -> None:
    handler = get_handler_for_file(
        "src/main/kotlin/com/initech/accounts/services/UserService.kt"
    )
    assert handler is not None
    assert handler.name == "kotlin"


def test_kotlin_handler_implements_protocol() -> None:
    assert isinstance(KotlinLanguageHandler(), LanguageHandler)


def test_python_still_registered() -> None:
    """Adding Kotlin must not have broken Python registration."""
    assert "python" in all_languages()
    py = get_handler_by_name("python")
    assert ".py" in py.source_extensions


# ---------------------------------------------------------------------------
# Parser — Initech-style Kotlin service patterns
# ---------------------------------------------------------------------------


_JWT_AUTH_SERVICE = '''\
package com.initech.accounts.services

import com.initech.accounts.config.JwtConfig
import org.springframework.stereotype.Service
import java.util.Date

@Service
class JwtAuthService(private val config: JwtConfig) {

    fun sign(
        emailAddress: String,
        claims: Map<String, String>,
        expiresOn: Date,
    ): String {
        val secret = config.authSecret
        return "$emailAddress:$expiresOn:${claims.size}"
    }

    fun decode(token: String): DecodedToken {
        val parts = token.split(":")
        return DecodedToken(subject = parts[0], claims = emptyMap())
    }
}

data class DecodedToken(val subject: String, val claims: Map<String, String>)
'''


def test_parser_finds_methods_in_changed_lines() -> None:
    # sign() body is around lines 13-19
    affected = analyzer.extract_affected(
        _JWT_AUTH_SERVICE,
        "src/main/kotlin/com/initech/accounts/services/JwtAuthService.kt",
        {15, 16, 17},
    )
    names = [fn.name for fn in affected]
    assert "sign" in names
    assert "decode" not in names


def test_parser_qualified_name_includes_package_and_class() -> None:
    affected = analyzer.extract_affected(
        _JWT_AUTH_SERVICE,
        "src/main/kotlin/com/initech/accounts/services/JwtAuthService.kt",
        {15},
    )
    assert len(affected) == 1
    assert affected[0].qualified_name == (
        "com.initech.accounts.services.JwtAuthService.sign"
    )


def test_parser_kind_is_method_for_class_function() -> None:
    affected = analyzer.extract_affected(
        _JWT_AUTH_SERVICE,
        "src/main/kotlin/com/initech/accounts/services/JwtAuthService.kt",
        {15},
    )
    assert affected[0].kind == "method"


def test_parser_snippet_includes_full_method() -> None:
    affected = analyzer.extract_affected(
        _JWT_AUTH_SERVICE,
        "src/main/kotlin/com/initech/accounts/services/JwtAuthService.kt",
        {15},
    )
    snippet = affected[0].source_code
    assert "fun sign(" in snippet
    assert "emailAddress: String" in snippet
    assert "return " in snippet


def test_parser_finds_both_methods_when_both_changed() -> None:
    affected = analyzer.extract_affected(
        _JWT_AUTH_SERVICE,
        "src/main/kotlin/com/initech/accounts/services/JwtAuthService.kt",
        {15, 22},
    )
    names = sorted(fn.name for fn in affected)
    assert names == ["decode", "sign"]


# ---------------------------------------------------------------------------
# Top-level functions (Kotlin allows free-standing functions)
# ---------------------------------------------------------------------------


_TOP_LEVEL_FN = '''\
package com.foo.utils

fun extractDomain(email: String): String {
    return email.substringAfter("@")
}

fun normalizeEmail(email: String): String {
    return email.trim().lowercase()
}
'''


def test_parser_finds_top_level_function() -> None:
    # extractDomain body is lines 3-5
    affected = analyzer.extract_affected(
        _TOP_LEVEL_FN, "src/main/kotlin/com/foo/utils/Email.kt", {3, 4},
    )
    assert len(affected) == 1
    assert affected[0].name == "extractDomain"
    assert affected[0].kind == "function"
    assert affected[0].qualified_name == "com.foo.utils.extractDomain"


# ---------------------------------------------------------------------------
# Nested classes
# ---------------------------------------------------------------------------


_NESTED = '''\
package com.foo

class FileService {

    fun getKey(name: String) = "key-$name"

    class Folders {
        fun getSignedUrl(key: String): String {
            return "https://example.com/$key"
        }

        fun getSignedUploadUrl(key: String, ttl: Long): String {
            return "https://example.com/upload/$key?ttl=$ttl"
        }
    }
}
'''


def test_parser_qualified_name_for_nested_class_method() -> None:
    # getSignedUrl is on lines 8-10
    affected = analyzer.extract_affected(
        _NESTED, "src/main/kotlin/com/foo/FileService.kt", {8, 9},
    )
    method_names = [fn.qualified_name for fn in affected]
    assert "com.foo.FileService.Folders.getSignedUrl" in method_names


def test_parser_nested_and_outer_changed_together() -> None:
    affected = analyzer.extract_affected(
        _NESTED, "src/main/kotlin/com/foo/FileService.kt", {5, 9},
    )
    names = sorted(fn.qualified_name for fn in affected)
    assert "com.foo.FileService.getKey" in names
    assert "com.foo.FileService.Folders.getSignedUrl" in names


# ---------------------------------------------------------------------------
# Object and companion object methods
# ---------------------------------------------------------------------------


_OBJECT_AND_COMPANION = '''\
package com.foo

object Singleton {
    fun doThing(): String = "thing"
}

class Widget {
    fun instanceMethod(): Int = 1

    companion object {
        fun factory(): Widget = Widget()
    }
}
'''


def test_parser_finds_object_method() -> None:
    affected = analyzer.extract_affected(
        _OBJECT_AND_COMPANION, "src/main/kotlin/com/foo/Misc.kt", {4},
    )
    names = [fn.qualified_name for fn in affected]
    assert "com.foo.Singleton.doThing" in names


def test_parser_finds_companion_object_method() -> None:
    affected = analyzer.extract_affected(
        _OBJECT_AND_COMPANION, "src/main/kotlin/com/foo/Misc.kt", {11},
    )
    names = [fn.qualified_name for fn in affected]
    assert "com.foo.Widget.Companion.factory" in names


# ---------------------------------------------------------------------------
# Annotations preserved in snippet
# ---------------------------------------------------------------------------


_WITH_SPRING_ANNOTATIONS = '''\
package com.foo.services

import org.springframework.stereotype.Service
import org.springframework.transaction.annotation.Transactional

@Service
class OrderService(private val repo: OrderRepository) {

    @Transactional
    fun placeOrder(request: OrderRequest): Order {
        return repo.save(Order.from(request))
    }
}
'''


def test_parser_snippet_includes_annotations() -> None:
    # placeOrder body is around lines 10-12
    affected = analyzer.extract_affected(
        _WITH_SPRING_ANNOTATIONS,
        "src/main/kotlin/com/foo/services/OrderService.kt",
        {10, 11},
    )
    assert len(affected) == 1
    snippet = affected[0].source_code
    assert "@Transactional" in snippet
    assert "fun placeOrder" in snippet


# ---------------------------------------------------------------------------
# Generated files are skipped
# ---------------------------------------------------------------------------


def test_apollo_generated_file_is_skipped() -> None:
    generated = '''\
// Generated by Apollo. Do not edit.
package com.foo.generated

class GeneratedQuery {
    fun execute(): String = "x"
}
'''
    affected = analyzer.extract_affected(
        generated, "src/main/kotlin/com/foo/generated/Query.kt",
        {3, 4, 5, 6},
    )
    assert affected == []


def test_is_generated_detects_apollo() -> None:
    src = "// Generated by Apollo\npackage com.foo\n"
    assert analyzer.is_generated(src)


def test_is_generated_returns_false_for_handwritten_code() -> None:
    assert not analyzer.is_generated(_JWT_AUTH_SERVICE)
    assert not analyzer.is_generated(_NESTED)


# ---------------------------------------------------------------------------
# collect_annotations (Stage 4 will use this for prompt tuning)
# ---------------------------------------------------------------------------


def test_collect_annotations_finds_service() -> None:
    result = analyzer.collect_annotations(
        _JWT_AUTH_SERVICE,
        "src/main/kotlin/com/initech/accounts/services/JwtAuthService.kt",
    )
    assert "JwtAuthService" in result
    assert "Service" in result["JwtAuthService"]


# ---------------------------------------------------------------------------
# Handler — test file conventions match Initech
# ---------------------------------------------------------------------------


def test_handler_suggests_initech_test_path() -> None:
    """Initech convention strips the com/initech/accounts/ prefix:
    tests live at unit/services/..., not unit/com/initech/accounts/services/...
    """
    h = KotlinLanguageHandler()
    suggested = h.suggest_test_path(
        "src/main/kotlin/com/initech/accounts/services/UserService.kt"
    )
    assert suggested == (
        "src/test/kotlin/unit/services/UserServiceTests.kt"
    )


def test_handler_suggests_for_top_level_file() -> None:
    """Non-Initech paths keep the full path under unit/ (fallback)."""
    h = KotlinLanguageHandler()
    suggested = h.suggest_test_path(
        "src/main/kotlin/com/foo/Email.kt"
    )
    # Non-Initech prefix → keep the com/foo/ structure
    assert suggested == "src/test/kotlin/unit/com/foo/EmailTests.kt"


def test_handler_candidate_paths_include_variants() -> None:
    h = KotlinLanguageHandler()
    candidates = h.candidate_test_paths(
        "src/main/kotlin/com/initech/accounts/services/Bar.kt"
    )
    # Default (with unit/ subdir, Initech prefix stripped)
    assert "src/test/kotlin/unit/services/BarTests.kt" in candidates
    # Without unit/ subdir
    assert "src/test/kotlin/services/BarTests.kt" in candidates
    # Singular variant
    assert "src/test/kotlin/unit/services/BarTest.kt" in candidates


def test_handler_recognizes_test_files() -> None:
    h = KotlinLanguageHandler()
    assert h.is_test_file("src/test/kotlin/unit/com/foo/BarTests.kt")
    assert h.is_test_file("src/test/kotlin/integration/com/foo/BarTests.kt")
    assert h.is_test_file("src/test/kotlin/unit/com/foo/BarTest.kt")
    assert h.is_test_file("some/random/dir/FooTest.kt")
    assert not h.is_test_file("src/main/kotlin/com/foo/Bar.kt")


def test_handler_skips_acceptance_tests() -> None:
    h = KotlinLanguageHandler()
    assert h.is_skipped_test_path(
        "src/test/kotlin/acceptance/com/foo/SomeAcceptanceTest.kt"
    )
    assert h.is_skipped_test_path(
        "src/test/kotlin/acceptance/features/MyScenario.feature"
    )
    assert not h.is_skipped_test_path(
        "src/test/kotlin/unit/com/foo/UserServiceTests.kt"
    )


def test_handler_temp_file_name_keeps_kt_extension() -> None:
    h = KotlinLanguageHandler()
    name = h.temp_test_file_name(
        "src/test/kotlin/unit/com/foo/BarTests.kt"
    )
    assert name == "_PRBotBarTests.kt"
    # Critical: must end in .kt so Gradle compiles it as Kotlin source
    assert name.endswith(".kt")


def test_handler_covers_backticked_english() -> None:
    """Conservative matcher (Stage 4b Option B): test name must start
    with ``methodName(`` — matching Initech's convention of writing
    test names as ``methodName() does X when Y``.
    """
    h = KotlinLanguageHandler()
    # Initech convention: methodName() at start of the backticked name
    assert h.covers("create() saves new user", "create")
    assert h.covers("decode() returns subject from token", "decode")
    # Tests that don't follow the convention — REJECTED conservatively
    assert not h.covers("decode returns subject from token", "decode")


def test_handler_covers_camelcase_prefix() -> None:
    h = KotlinLanguageHandler()
    assert h.covers("testCreate", "create")
    assert h.covers("testCreateReturnsId", "create")


def test_handler_covers_rejects_unrelated() -> None:
    h = KotlinLanguageHandler()
    assert not h.covers("verifySomethingElse", "createUser")
    assert not h.covers("", "createUser")
    assert not h.covers("test", "createUser")


# ---------------------------------------------------------------------------
# Stage 4b boundaries — incremental + fix now work
# ---------------------------------------------------------------------------


def test_all_stage_4b_prompts_succeed() -> None:
    """Stage 4b ships incremental merge + fix loop prompts. All three
    prompt categories should now return non-empty strings.
    """
    h = KotlinLanguageHandler()

    # All three system prompts work
    assert h.system_prompt_fresh()
    assert h.system_prompt_incremental()
    assert h.system_prompt_fix()

    # merge_new_tests no longer raises
    result = h.merge_new_tests("class Foo {\n}\n", "")
    assert isinstance(result, str)


def test_build_test_command_works_in_stage_3() -> None:
    """In Stage 3, build_test_command should produce a real Gradle command
    instead of raising NotImplementedError.
    """
    h = KotlinLanguageHandler()
    cmd = h.build_test_command(["test.kt"], "/repo")
    assert cmd[0] == "./gradlew"
    assert "--console=plain" in cmd


def test_parse_existing_tests_works_in_stage_4b() -> None:
    """parse_existing_tests previously raised NotImplementedError. Now
    it returns a list of KotlinTestFunction (empty for class declarations
    with no tests).
    """
    h = KotlinLanguageHandler()
    result = h.parse_existing_tests("package unit.foo\nclass FooTests {\n}\n")
    assert isinstance(result, list)
    assert result == []  # no @Test functions in this file


# ---------------------------------------------------------------------------
# Collection error markers
# ---------------------------------------------------------------------------


def test_collection_error_markers_exclude_compile_failures_post4() -> None:
    """v0.2.0a6.post4 changed collection_error_markers() to NOT include
    compile errors. Compile errors are usually fixable by Claude
    (unresolved references, wrong arguments, etc.) so the fix loop
    should engage on them. Only true environment errors trigger the
    bailout now.
    """
    h = KotlinLanguageHandler()
    markers = h.collection_error_markers()
    # Compile errors are NOT here anymore
    assert "Task :compileTestKotlin FAILED" not in markers
    assert "Compilation error" not in markers
    # Environment errors still are
    assert "Could not resolve all files for configuration" in markers


def test_handler_collection_error_markers_works() -> None:
    """Sanity: the handler exposes the runner's markers."""
    h = KotlinLanguageHandler()
    markers = h.collection_error_markers()
    assert isinstance(markers, tuple)
    assert len(markers) > 0


# ---------------------------------------------------------------------------
# Diff reader integration
# ---------------------------------------------------------------------------


def test_diff_reader_includes_kotlin_files(tmp_path) -> None:
    from pr_test_automator_local.config import LocalTestConfig
    from pr_test_automator_local.steps.local_diff_reader import (
        LocalDiffReader,
    )

    reader = LocalDiffReader(LocalTestConfig(repo_path=str(tmp_path)))
    # Kotlin sources eligible
    assert reader._is_eligible_source(
        "src/main/kotlin/com/foo/Bar.kt"
    )
    # Kotlin tests excluded
    assert not reader._is_eligible_source(
        "src/test/kotlin/unit/com/foo/BarTests.kt"
    )
    # Python still works (regression)
    assert reader._is_eligible_source("src/foo.py")
    assert not reader._is_eligible_source("src/test_foo.py")
    # Unknown extensions still excluded
    assert not reader._is_eligible_source("README.md")
    # Java is NOT registered anymore (Java skeleton dropped)
    assert not reader._is_eligible_source(
        "src/main/java/com/foo/Bar.java"
    )
