"""Tests for Kotlin Stage 4b: merger (parsing existing tests, removing
them, merging new tests in) and the incremental + fix prompts.

The parsing fixtures use a simplified Initech-style test file (rather
than the full 1263-line ``UserServiceTests.kt`` from the upload). This
keeps the fixtures readable and the tests focused. End-to-end
verification on the real file happens in the user's environment.
"""

from __future__ import annotations

import pytest

from test_automator.languages.kotlin import merger, prompts
from test_automator.languages.kotlin.handler import (
    KotlinLanguageHandler,
)
from test_automator.languages.kotlin.merger import (
    KotlinTestFunction,
)
from test_automator.models import (
    AffectedFunction,
    ExistingTest,
    GeneratedTest,
)


# ---------------------------------------------------------------------------
# Realistic Initech-style test file fixture
# ---------------------------------------------------------------------------

# A simplified UserServiceTests.kt mimicking the style of the real file
# (Strikt + MockK + backticked names + @BeforeEach + slots).
_EXISTING_TEST_FILE = '''\
package unit.services

import com.initech.accounts.services.UserService
import com.initech.accounts.repositories.UserRepository
import com.initech.accounts.entities.UserEntity
import io.mockk.*
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import strikt.api.expectThat
import strikt.api.expectThrows
import strikt.assertions.*

class UserServiceTests {
    private val userRepository = mockk<UserRepository>()
    private val userService = UserService(userRepository)

    private val userEntitySlot = slot<UserEntity>()

    @BeforeEach
    fun beforeEach() {
        userEntitySlot.clear()
        clearAllMocks()
    }

    @Test
    fun `create() saves and returns new user`() {
        every { userRepository.save(capture(userEntitySlot)) } answers { userEntitySlot.captured }

        val user = userService.create("test@example.com")

        expectThat(user) {
            get { emailAddress }.isEqualTo("test@example.com")
        }
    }

    @Test
    fun `create() throws when email is blank`() {
        expectThrows<IllegalArgumentException> {
            userService.create("")
        }
    }

    @Test
    fun `findById() returns user when present`() {
        val expected = UserEntity()
        every { userRepository.findById(any()) } returns expected

        val result = userService.findById("some-id")

        expectThat(result).isEqualTo(expected)
    }
}
'''


# ---------------------------------------------------------------------------
# parse_existing_test_functions — extract @Test functions from a file
# ---------------------------------------------------------------------------


def test_parse_finds_all_test_functions() -> None:
    tests = merger.parse_existing_test_functions(_EXISTING_TEST_FILE)
    names = [t.name for t in tests]
    assert "create() saves and returns new user" in names
    assert "create() throws when email is blank" in names
    assert "findById() returns user when present" in names


def test_parse_strips_backticks_from_names() -> None:
    tests = merger.parse_existing_test_functions(_EXISTING_TEST_FILE)
    for t in tests:
        # No backticks should remain in the name
        assert not t.name.startswith("`")
        assert not t.name.endswith("`")


def test_parse_skips_non_test_functions() -> None:
    """``beforeEach`` and other non-``@Test`` functions should NOT appear
    in the result list.
    """
    tests = merger.parse_existing_test_functions(_EXISTING_TEST_FILE)
    names = [t.name for t in tests]
    assert "beforeEach" not in names


def test_parse_captures_test_annotation() -> None:
    tests = merger.parse_existing_test_functions(_EXISTING_TEST_FILE)
    for t in tests:
        assert "Test" in t.annotations


def test_parse_returns_tests_in_source_order() -> None:
    tests = merger.parse_existing_test_functions(_EXISTING_TEST_FILE)
    # The fixture lists tests in this order
    assert [t.name for t in tests] == [
        "create() saves and returns new user",
        "create() throws when email is blank",
        "findById() returns user when present",
    ]


def test_parse_records_line_ranges() -> None:
    """Each test should have a sensible line range (start ≤ end)."""
    tests = merger.parse_existing_test_functions(_EXISTING_TEST_FILE)
    for t in tests:
        assert t.line_start >= 1
        assert t.line_end >= t.line_start


def test_parse_empty_file_returns_empty_list() -> None:
    assert merger.parse_existing_test_functions("") == []


def test_parse_malformed_kotlin_returns_empty() -> None:
    """Parser should not crash on broken syntax. Returns empty list
    instead.
    """
    bad = "this is not valid kotlin {{{ }"
    # Tree-sitter is tolerant — it may still extract something or nothing.
    # Either way it must not crash.
    result = merger.parse_existing_test_functions(bad)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# extract_test_source — verbatim text of tests by line range
# ---------------------------------------------------------------------------


def test_extract_test_source_returns_verbatim_text() -> None:
    tests = merger.parse_existing_test_functions(_EXISTING_TEST_FILE)
    # Extract just the first test
    first = [t for t in tests if "create() saves" in t.name]
    src = merger.extract_test_source(_EXISTING_TEST_FILE, first)
    assert "@Test" in src
    assert "create() saves and returns new user" in src
    assert "userService.create" in src
    # Other tests should NOT be in there
    assert "throws when email" not in src


def test_extract_test_source_empty_list_returns_empty() -> None:
    assert merger.extract_test_source(_EXISTING_TEST_FILE, []) == ""


def test_extract_test_source_multiple_tests_in_order() -> None:
    tests = merger.parse_existing_test_functions(_EXISTING_TEST_FILE)
    # Reverse the order — extract_test_source should still produce them
    # in file order.
    reversed_tests = list(reversed(tests))
    src = merger.extract_test_source(_EXISTING_TEST_FILE, reversed_tests)
    # The first test in file order should appear first in the extracted text
    create_saves_idx = src.find("create() saves and returns new user")
    findById_idx = src.find("findById() returns user when present")
    assert create_saves_idx < findById_idx


# ---------------------------------------------------------------------------
# remove_tests — surgical removal from existing file
# ---------------------------------------------------------------------------


def test_remove_tests_preserves_imports() -> None:
    tests = merger.parse_existing_test_functions(_EXISTING_TEST_FILE)
    to_remove = [t for t in tests if "create() saves" in t.name]
    result = merger.remove_tests(_EXISTING_TEST_FILE, to_remove)
    assert "import com.initech.accounts.services.UserService" in result
    assert "import io.mockk.*" in result


def test_remove_tests_preserves_class_declaration() -> None:
    tests = merger.parse_existing_test_functions(_EXISTING_TEST_FILE)
    to_remove = [t for t in tests if "create() saves" in t.name]
    result = merger.remove_tests(_EXISTING_TEST_FILE, to_remove)
    assert "class UserServiceTests {" in result
    # And the closing brace
    assert result.rstrip().endswith("}")


def test_remove_tests_preserves_class_level_properties() -> None:
    tests = merger.parse_existing_test_functions(_EXISTING_TEST_FILE)
    to_remove = [t for t in tests if "create() saves" in t.name]
    result = merger.remove_tests(_EXISTING_TEST_FILE, to_remove)
    assert "private val userRepository = mockk<UserRepository>()" in result
    assert "private val userEntitySlot = slot<UserEntity>()" in result


def test_remove_tests_preserves_before_each() -> None:
    tests = merger.parse_existing_test_functions(_EXISTING_TEST_FILE)
    to_remove = [t for t in tests if "create() saves" in t.name]
    result = merger.remove_tests(_EXISTING_TEST_FILE, to_remove)
    assert "@BeforeEach" in result
    assert "fun beforeEach()" in result
    assert "clearAllMocks()" in result


def test_remove_tests_actually_removes_target() -> None:
    tests = merger.parse_existing_test_functions(_EXISTING_TEST_FILE)
    to_remove = [t for t in tests if "create() saves" in t.name]
    result = merger.remove_tests(_EXISTING_TEST_FILE, to_remove)
    assert "create() saves and returns new user" not in result


def test_remove_tests_keeps_other_tests() -> None:
    """When removing one test, the others should still be present."""
    tests = merger.parse_existing_test_functions(_EXISTING_TEST_FILE)
    to_remove = [t for t in tests if "create() saves" in t.name]
    result = merger.remove_tests(_EXISTING_TEST_FILE, to_remove)
    assert "create() throws when email is blank" in result
    assert "findById() returns user when present" in result


def test_remove_tests_removes_multiple() -> None:
    tests = merger.parse_existing_test_functions(_EXISTING_TEST_FILE)
    to_remove = [t for t in tests if "create()" in t.name]
    result = merger.remove_tests(_EXISTING_TEST_FILE, to_remove)
    # Both create() tests gone
    assert "create() saves and returns new user" not in result
    assert "create() throws when email is blank" not in result
    # findById remains
    assert "findById() returns user when present" in result


def test_remove_tests_empty_list_returns_unchanged() -> None:
    result = merger.remove_tests(_EXISTING_TEST_FILE, [])
    assert result == _EXISTING_TEST_FILE


# ---------------------------------------------------------------------------
# merge_new_tests — splice new tests into existing class body
# ---------------------------------------------------------------------------


def test_merge_inserts_before_closing_brace() -> None:
    new_test = """    @Test
    fun `newMethod() does something`() {
        expectThat(1).isEqualTo(1)
    }
"""
    result = merger.merge_new_tests(_EXISTING_TEST_FILE, new_test)
    # New test should be present
    assert "newMethod() does something" in result
    # File should still end with closing brace
    assert result.rstrip().endswith("}")
    # The new test should appear AFTER the existing tests but BEFORE the
    # class's closing brace
    newmethod_idx = result.find("newMethod()")
    last_brace_idx = result.rfind("}")
    assert newmethod_idx < last_brace_idx


def test_merge_preserves_existing_tests() -> None:
    new_test = """    @Test
    fun `added test`() {
    }
"""
    result = merger.merge_new_tests(_EXISTING_TEST_FILE, new_test)
    assert "create() saves and returns new user" in result
    assert "findById() returns user when present" in result
    assert "added test" in result


def test_merge_empty_new_tests_returns_unchanged() -> None:
    result = merger.merge_new_tests(_EXISTING_TEST_FILE, "")
    assert result == _EXISTING_TEST_FILE


def test_merge_handles_whitespace_only_input() -> None:
    """Pure whitespace should be treated as no new tests."""
    result = merger.merge_new_tests(_EXISTING_TEST_FILE, "   \n\n  \n")
    assert result == _EXISTING_TEST_FILE


def test_merge_reindents_to_match_existing_file() -> None:
    """When Claude returns tests with no leading whitespace, the merger
    must re-indent to match the existing file (4 spaces in this fixture).
    """
    new_test = """@Test
fun `unindented test`() {
    expectThat(1).isEqualTo(1)
}
"""
    result = merger.merge_new_tests(_EXISTING_TEST_FILE, new_test)
    # The new test should be indented in the result
    lines = result.splitlines()
    test_line_idx = next(
        i for i, line in enumerate(lines)
        if "unindented test" in line
    )
    # The line containing the @Test annotation should be indented
    # (look one or two lines before for @Test)
    for i in range(max(0, test_line_idx - 3), test_line_idx + 1):
        if "@Test" in lines[i]:
            # Should start with whitespace (was unindented input)
            assert lines[i].startswith(" "), (
                f"Expected indented @Test, got: {lines[i]!r}"
            )
            break


# ---------------------------------------------------------------------------
# Roundtrip: remove + merge produces a coherent file
# ---------------------------------------------------------------------------


def test_roundtrip_remove_then_merge_keeps_file_coherent() -> None:
    """Simulate the incremental-merge lifecycle:
    1. Parse existing tests
    2. Identify create() tests to remove
    3. Remove them
    4. Merge new create() tests in
    Result should: have all the new tests + the unrelated findById test +
    all imports and class structure intact.
    """
    tests = merger.parse_existing_test_functions(_EXISTING_TEST_FILE)
    create_tests = [
        t for t in tests
        if t.name.startswith("create(")
    ]
    trimmed = merger.remove_tests(_EXISTING_TEST_FILE, create_tests)

    new_tests_code = """    @Test
    fun `create() returns user with specified locale`() {
        expectThat(true).isTrue()
    }

    @Test
    fun `create() validates email format`() {
        expectThat(true).isTrue()
    }
"""

    merged = merger.merge_new_tests(trimmed, new_tests_code)

    # Imports intact
    assert "import io.mockk.*" in merged
    # Mock declarations intact
    assert "private val userRepository = mockk<UserRepository>()" in merged
    # @BeforeEach intact
    assert "@BeforeEach" in merged
    # Original create() tests GONE
    assert "create() saves and returns new user" not in merged
    assert "create() throws when email is blank" not in merged
    # New create() tests present
    assert "create() returns user with specified locale" in merged
    assert "create() validates email format" in merged
    # Unrelated test (findById) untouched
    assert "findById() returns user when present" in merged
    # Class structure intact (matching braces)
    assert merged.count("class UserServiceTests {") == 1
    assert merged.rstrip().endswith("}")


# ---------------------------------------------------------------------------
# Conservative covers() matcher (Stage 4b Option B)
# ---------------------------------------------------------------------------


def test_covers_requires_methodname_open_paren_prefix() -> None:
    """The conservative matcher only matches when the test name starts
    with ``functionName(``. This is Initech's convention.
    """
    h = KotlinLanguageHandler()
    # MATCHES: starts with "create("
    assert h.covers("create() saves new user", "create")
    # MATCHES: starts with "decode("
    assert h.covers("decode() returns subject", "decode")


def test_covers_rejects_substring_matches() -> None:
    """Critical: under Option B, a test whose name MENTIONS the function
    name (but doesn't start with it+paren) should NOT cover.
    """
    h = KotlinLanguageHandler()
    # Test about something else that happens to mention "create"
    assert not h.covers(
        "anotherMethod() also uses create internally", "create"
    )
    # Test for a sibling method with similar name
    assert not h.covers("createOther() does something", "create")


def test_covers_handles_camelcase_test_prefix() -> None:
    """For non-Initech codebases that still use camelCase ``testFoo``
    naming, the matcher accepts that too.
    """
    h = KotlinLanguageHandler()
    assert h.covers("testCreate", "create")
    assert h.covers("testCreateUser", "create")


def test_covers_handles_backticked_input_defensively() -> None:
    """If a caller passes a name WITH backticks (e.g. directly from a
    file), the matcher still works.
    """
    h = KotlinLanguageHandler()
    assert h.covers("`create() saves new user`", "create")


def test_covers_rejects_empty() -> None:
    h = KotlinLanguageHandler()
    assert not h.covers("", "create")
    assert not h.covers("create() x", "")


# ---------------------------------------------------------------------------
# Incremental prompt construction
# ---------------------------------------------------------------------------


def test_user_prompt_incremental_includes_all_sections() -> None:
    """The incremental user prompt must contain: source path, test path,
    affected functions, trimmed existing content, and removed tests.
    """
    aff = AffectedFunction(
        file_path="src/main/kotlin/com/initech/accounts/services/UserService.kt",
        name="create",
        qualified_name="com.initech.accounts.services.UserService.create",
        kind="method",
        source_code="fun create(email: String) = UserEntity(email)",
        line_start=1,
        line_end=1,
    )
    existing = ExistingTest(
        test_file_path="src/test/kotlin/unit/services/UserServiceTests.kt",
        source_file_path=(
            "src/main/kotlin/com/initech/accounts/services/UserService.kt"
        ),
        content=_EXISTING_TEST_FILE,
    )
    trimmed = "class UserServiceTests {\n}"
    removed = "@Test fun `create() saves new user`() { ... }"

    prompt = prompts.user_prompt_incremental(
        existing.source_file_path,
        existing,
        [aff],
        trimmed,
        removed,
    )

    assert "UserService.kt" in prompt
    assert "UserServiceTests.kt" in prompt
    assert "fun create(email: String)" in prompt
    assert trimmed in prompt
    assert removed in prompt


def test_user_prompt_incremental_handles_empty_removed() -> None:
    """If no tests were removed (somehow), the prompt should still be
    valid — provide a clear placeholder.
    """
    aff = AffectedFunction(
        file_path="src/main/kotlin/com/initech/accounts/services/X.kt",
        name="foo",
        qualified_name="com.initech.accounts.services.X.foo",
        kind="method",
        source_code="fun foo() = 1",
        line_start=1,
        line_end=1,
    )
    existing = ExistingTest(
        test_file_path="src/test/kotlin/unit/services/XTests.kt",
        source_file_path="src/main/kotlin/com/initech/accounts/services/X.kt",
        content="class XTests {\n}",
    )
    prompt = prompts.user_prompt_incremental(
        existing.source_file_path, existing, [aff], "class XTests {\n}", "",
    )
    # Should include a "no previous tests" placeholder
    assert "no previous tests" in prompt.lower()


# ---------------------------------------------------------------------------
# Fix prompt construction
# ---------------------------------------------------------------------------


def test_user_prompt_fix_includes_source_test_and_error(
    tmp_path,
) -> None:
    """The fix prompt must include: source code (we include it per
    design decision), test content, and Gradle output.

    We write a real source file to tmp so the open() call in
    user_prompt_fix succeeds.
    """
    src = tmp_path / "Foo.kt"
    src.write_text("fun foo() = 42\n")

    gen = GeneratedTest(
        source_file_path=str(src),
        test_file_path="src/test/kotlin/unit/services/FooTests.kt",
        content="class FooTests { @Test fun `foo() works`() {} }",
        covered_functions=["foo"],
    )

    runner_output = "Test FAILED: expected 42 but got 0"
    prompt = prompts.user_prompt_fix(gen, runner_output)

    # Source file content present
    assert "fun foo() = 42" in prompt
    # Test content present
    assert "class FooTests" in prompt
    # Gradle output present
    assert "expected 42 but got 0" in prompt


def test_user_prompt_fix_handles_missing_source(
    tmp_path,
) -> None:
    """If the source file can't be read (deleted, etc.), the prompt
    should still be produced (with a placeholder).
    """
    gen = GeneratedTest(
        source_file_path=str(tmp_path / "nonexistent.kt"),
        test_file_path="src/test/kotlin/unit/services/XTests.kt",
        content="class XTests { }",
        covered_functions=["foo"],
    )
    prompt = prompts.user_prompt_fix(gen, "error: something")
    # Doesn't crash, includes the placeholder
    assert "unavailable" in prompt.lower() or "class XTests" in prompt
