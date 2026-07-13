"""Tests for the Kotlin source extractor.

The fixtures in this file come from REAL Claude Code responses captured
during the v0.2.0a6 run on accounts-service. They contain the actual
prose/markdown noise that broke the previous version.
"""

from __future__ import annotations

import pytest

from test_automator.languages.kotlin.extractor import (
    ExtractionError,
    extract_kotlin_file,
    extract_kotlin_tests_block,
)


# ---------------------------------------------------------------------------
# Real-world bad output captured from v0.2.0a6 run
# ---------------------------------------------------------------------------


# This is the EXACT prose/code mix that came back for the incremental
# merge case (SendDynamicReportPackageEmailHandler).
INCREMENTAL_NOISY_RESPONSE = """\
Now I have everything I need. Let me generate the tests.

---

@Test
fun `handle() uses linkExpirationDays from settings when modifyLinkExpirationEnabled is true`() {
    val orgUnitId = createValidOrgUnitId()
    every { orgUnitRepository.findById(any()) } returns Optional.of(orgUnit)

    handler.handle(command)

    verify(exactly = 1) { repository.save(any()) }
}

@Test
fun `validateEmails() splits emails on semicolons`() {
    val result = handler.validateEmails("a@test.com;b@test.com")
    expectThat(result).containsExactly("a@test.com", "b@test.com")
}

---

**Notes on what each group covers:**

- **Link expiration** — the test exercises the branch...
- **`validateEmails()`** — covers semicolon splitting...
"""

# This is the EntityMappers case — Claude returned permission prose
# with NO actual Kotlin source at all.
TOOL_PERMISSION_NOISE = """\
The file write needs your approval. Once you approve, the generated
`EntityMappersTests.kt` will be saved at `src/test/kotlin/unit/avro/EntityMappersTests.kt`.

Here's a summary of what the file covers:
**Pattern**: Follows the existing EntityMapperTests.kt style exactly.
**`getClientSettings()` — 13 tests:**
- Happy path: all fields mapped
- Null cases: notes, csiId, billingAccount
"""

# A markdown-fenced complete file response
FRESH_FENCED_RESPONSE = """\
```kotlin
package unit.services

import com.initech.accounts.services.Calculator
import io.mockk.*
import org.junit.jupiter.api.Test
import strikt.api.expectThat
import strikt.assertions.*

class CalculatorTests {
    private val calculator = Calculator()

    @Test
    fun `multiply() returns product of two integers`() {
        expectThat(calculator.multiply(3, 4)).isEqualTo(12)
    }
}
```
"""

# A response with prose preamble and trailing prose, no fences
FRESH_NOISY_RESPONSE = """\
I'll write a test file for the Calculator class. Here's my approach:

The test covers the happy path and verifies multiplication.

package unit.services

import com.initech.accounts.services.Calculator
import io.mockk.*
import org.junit.jupiter.api.Test
import strikt.api.expectThat
import strikt.assertions.*

class CalculatorTests {
    private val calculator = Calculator()

    @Test
    fun `multiply() returns product`() {
        expectThat(calculator.multiply(3, 4)).isEqualTo(12)
    }
}

That covers the basic case. Let me know if you want me to add more tests
for edge cases like zero or negative numbers.
"""


# ---------------------------------------------------------------------------
# Fresh-file extractor
# ---------------------------------------------------------------------------


def test_extract_fresh_strips_markdown_fences() -> None:
    result = extract_kotlin_file(FRESH_FENCED_RESPONSE)
    # No fence markers in output
    assert "```" not in result
    # Has the actual code
    assert "package unit.services" in result
    assert "class CalculatorTests" in result
    # Ends with the class's closing brace
    assert result.rstrip().endswith("}")


def test_extract_fresh_strips_prose_preamble_and_postamble() -> None:
    """The noisy response has prose before AND after the Kotlin block.
    Both should be stripped.
    """
    result = extract_kotlin_file(FRESH_NOISY_RESPONSE)
    # No preamble
    assert "I'll write a test file" not in result
    assert "Here's my approach" not in result
    # No postamble
    assert "That covers the basic case" not in result
    assert "Let me know" not in result
    # The actual code IS there
    assert result.startswith("package unit.services")
    assert "class CalculatorTests" in result
    assert result.rstrip().endswith("}")


def test_extract_fresh_raises_on_pure_prose() -> None:
    """The EntityMappers case — Claude returned permission prose with
    no `package` line. Must surface as ExtractionError, not silently
    write prose to disk.
    """
    with pytest.raises(ExtractionError, match="no `package` declaration"):
        extract_kotlin_file(TOOL_PERMISSION_NOISE)


def test_extract_fresh_raises_on_truncated_class() -> None:
    """If Claude's response has `package` but the class is unclosed
    (response got cut off), raise rather than producing a partial file.
    """
    truncated = """\
package unit.services
class Foo {
    @Test fun `x`() {
        // never closed
"""
    with pytest.raises(ExtractionError, match="closing brace"):
        extract_kotlin_file(truncated)


def test_extract_fresh_handles_braces_in_strings() -> None:
    """Strings containing braces shouldn't confuse the brace counter."""
    src = '''\
package unit.foo
class Bar {
    val template = "{ this looks like a brace }"
    val triple = """multiline { brace inside } """
}
'''
    result = extract_kotlin_file(src)
    assert "class Bar {" in result
    assert result.rstrip().endswith("}")
    # The string content shouldn't have been treated as a class close
    assert '"{ this looks like a brace }"' in result


def test_extract_fresh_handles_braces_in_comments() -> None:
    """Braces inside `//` and `/* */` comments shouldn't count."""
    src = """\
package unit.foo

class Bar {
    // this } is in a comment
    /* and this } too */
    fun work() = 1
}
"""
    result = extract_kotlin_file(src)
    assert "fun work() = 1" in result
    assert result.rstrip().endswith("}")


def test_extract_fresh_passes_through_clean_input() -> None:
    """No-prose, no-fences input should round-trip unchanged (modulo
    final newline).
    """
    clean = """\
package unit.services

class CalculatorTests {
    @Test fun `add works`() {}
}
"""
    result = extract_kotlin_file(clean)
    # Same content, normalized trailing newline
    assert result.strip() == clean.strip()


# ---------------------------------------------------------------------------
# Incremental tests-block extractor
# ---------------------------------------------------------------------------


def test_extract_incremental_strips_prose_around_test_blocks() -> None:
    """The real production failure case — incremental response has
    'Now I have everything I need. Let me generate...' prose before the
    @Test blocks and '---\\n**Notes...**' prose after.
    """
    result = extract_kotlin_tests_block(INCREMENTAL_NOISY_RESPONSE)

    # No prose
    assert "Now I have everything I need" not in result
    assert "Notes on what each group covers" not in result
    assert "---" not in result

    # The actual tests are there
    assert "fun `handle() uses linkExpirationDays" in result
    assert "fun `validateEmails() splits emails on semicolons`" in result

    # @Test annotations preserved
    assert "@Test" in result


def test_extract_incremental_returns_complete_test_blocks() -> None:
    """Each extracted block should include the full body, not get
    truncated mid-function.
    """
    result = extract_kotlin_tests_block(INCREMENTAL_NOISY_RESPONSE)
    # The validateEmails test has expectThat in its body
    assert "expectThat(result).containsExactly" in result
    # And the handle test has its full body
    assert "verify(exactly = 1)" in result


def test_extract_incremental_strips_markdown_fences() -> None:
    fenced = """\
```kotlin
@Test
fun `foo() works`() {
    expectThat(true).isTrue()
}
```
"""
    result = extract_kotlin_tests_block(fenced)
    assert "```" not in result
    assert "@Test" in result
    assert "fun `foo() works`()" in result


def test_extract_incremental_raises_on_pure_prose() -> None:
    """If the response has zero @Test annotations, raise loudly."""
    prose_only = (
        "I've reviewed the source code. The function looks well-tested "
        "already and I don't think additional tests are needed."
    )
    with pytest.raises(ExtractionError, match="no `@Test` annotation"):
        extract_kotlin_tests_block(prose_only)


def test_extract_incremental_handles_braces_in_test_body() -> None:
    """A test body can have nested braces — Strikt block-form
    assertions use them.
    """
    src = """\
@Test
fun `block assertion`() {
    expectThat(obj) {
        get { id }.isEqualTo(42)
        get { name }.isEqualTo("foo")
    }
}
"""
    result = extract_kotlin_tests_block(src)
    # The nested braces in the assertion block shouldn't have closed
    # the function early
    assert 'get { name }.isEqualTo("foo")' in result
    assert result.rstrip().endswith("}")


def test_extract_incremental_handles_multiple_tests() -> None:
    src = """\
@Test
fun `first test`() {
    expectThat(1).isEqualTo(1)
}

@Test
fun `second test`() {
    expectThat(2).isEqualTo(2)
}

@Test
fun `third test`() {
    expectThat(3).isEqualTo(3)
}
"""
    result = extract_kotlin_tests_block(src)
    assert "fun `first test`" in result
    assert "fun `second test`" in result
    assert "fun `third test`" in result


def test_extract_incremental_stops_at_prose_separator() -> None:
    """When prose resumes (e.g., '---' separator), the extractor should
    stop emitting blocks rather than trying to absorb the prose.
    """
    src = """\
@Test
fun `test one`() {
    expectThat(1).isEqualTo(1)
}

---

This concludes my recommendations. The above test covers the happy path.
"""
    result = extract_kotlin_tests_block(src)
    assert "fun `test one`" in result
    assert "concludes my recommendations" not in result


# ---------------------------------------------------------------------------
# Handler integration
# ---------------------------------------------------------------------------


def test_handler_extract_code_dispatches_by_mode() -> None:
    from test_automator.languages.kotlin.handler import (
        KotlinLanguageHandler,
    )

    h = KotlinLanguageHandler()

    # Fresh mode — needs full file with package
    fresh = h.extract_code(FRESH_FENCED_RESPONSE, mode="fresh")
    assert "package unit.services" in fresh
    assert "```" not in fresh

    # Incremental mode — just @Test blocks
    inc = h.extract_code(INCREMENTAL_NOISY_RESPONSE, mode="incremental")
    assert "@Test" in inc
    assert "Now I have everything I need" not in inc


def test_handler_extract_code_raises_on_unknown_mode() -> None:
    from test_automator.languages.kotlin.handler import (
        KotlinLanguageHandler,
    )

    h = KotlinLanguageHandler()
    with pytest.raises(ValueError, match="Unknown extraction mode"):
        h.extract_code("package foo\nclass Bar {}", mode="bogus")


# ---------------------------------------------------------------------------
# v0.2.0 fix: multi-fence responses where the first fence is a snippet
# ---------------------------------------------------------------------------


def test_extracts_from_fence_with_package_when_multiple_fences() -> None:
    """Reproduces the user's actual failure: Claude returned a response
    with TWO fences. The first fence quoted the source function being
    discussed (no package declaration). The second fence had the actual
    fixed test file (with package declaration).

    Before the fix, the extractor took the first fence and crashed
    because it had no `package` line. The new logic picks the fence
    containing the `package` declaration.
    """
    from test_automator.languages.kotlin.extractor import (
        extract_kotlin_file,
    )

    response = '''Looking at the source code for `percentageOf`:

```kotlin
infix fun Int.percentageOf(value: Int) = when {
    this == 0 -> 0
    this < 0 -> 0
    else -> value * 100 / this
}
```

Now the actual fixed test file:

```kotlin
package unit.test

import org.junit.jupiter.api.Test
import strikt.api.expectThat
import strikt.assertions.isEqualTo

class ExtensionsTests {
    @Test
    fun `percentageOf returns 0 when receiver is 0`() {
        expectThat(0.percentageOf(50)).isEqualTo(0)
    }
}
```
'''

    result = extract_kotlin_file(response)
    assert "package unit.test" in result
    assert "class ExtensionsTests" in result
    # The source-function snippet should NOT be in the extracted code
    assert "infix fun Int.percentageOf" not in result


def test_falls_back_to_largest_fence_when_no_package_anywhere() -> None:
    """If NO fence has a package declaration, the extractor used to
    crash. Now it falls back to the largest fence so the next stage
    can still try to make sense of it (and may still fail downstream,
    but the failure is more informative).
    """
    from test_automator.languages.kotlin.extractor import (
        extract_kotlin_file, ExtractionError,
    )

    # No package declaration anywhere — should still raise, but via
    # the package-check at the next layer, not by silently picking
    # the wrong fence.
    response = '''Here's a snippet:

```kotlin
val x = 1
```

And another:

```kotlin
val y = 2
```
'''
    # This should still raise ExtractionError (no package found)
    # — but we don't crash earlier when trying to find the right fence
    try:
        extract_kotlin_file(response)
        assert False, "Should have raised ExtractionError"
    except ExtractionError as e:
        # The error message should be the "no package declaration" one,
        # not a fence-parsing error
        assert "package" in str(e).lower()


def test_braces_inside_backtick_test_name_dont_break_extraction():
    """Kotlin escaped identifiers (backtick test names) may contain
    literal braces; they must not be counted as real blocks, or a
    complete file is falsely rejected as truncated."""
    from test_automator.languages.kotlin.extractor import extract_kotlin_file

    src = (
        "package unit.handlers\n\n"
        "class FooTests {\n"
        "    @Test\n"
        "    fun `saveLog not called when {precondition}`() {\n"
        "        verify(exactly = 0) { svc.saveLog(any()) }\n"
        "    }\n"
        "}\n"
    )
    out = extract_kotlin_file(src)
    assert out.rstrip().endswith("}")
    assert "saveLog not called" in out
