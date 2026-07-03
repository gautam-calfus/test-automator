"""Core tests for the Java language plugin (v0.3.0a1).

Coverage targets the high-value cases:
- Analyzer correctly identifies changed methods in Spring services
- Class signature extraction strips method bodies (compact mode)
- Extractor handles markdown fences, prose preamble, multi-fence responses
- Handler maps source paths to test paths per Acme conventions
- Build tool detection picks Maven over Gradle, prefers wrappers
- Test output parsing for both Maven Surefire and Gradle test-logger

These tests run against synthetic fixtures so they're fast and
deterministic. The real validation happens on Acme's actual codebase.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from pr_test_automator_local.languages.java import JavaLanguageHandler
from pr_test_automator_local.languages.java import analyzer, extractor, runner


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------

_SPRING_SERVICE_SRC = """\
package com.acme.service;

import org.springframework.stereotype.Service;
import org.springframework.beans.factory.annotation.Autowired;

@Service
public class CMService {
    private final Daos daos;
    private final UserService userService;

    @Autowired
    public CMService(Daos daos, UserService userService) {
        this.daos = daos;
        this.userService = userService;
    }

    public String fetchById(int id) {
        return daos.getQuestionDao().fetchById(id);
    }

    public void doSomething(String input) {
        // Some implementation
        if (input == null) {
            throw new IllegalArgumentException("null input");
        }
        userService.update(input);
        daos.getAuditDao().log(input);
    }

    private boolean helper(String s) {
        return s != null && !s.isEmpty();
    }
}
"""


def test_analyzer_finds_method_overlapping_changed_lines() -> None:
    """Changed line inside ``doSomething`` should yield that method."""
    # doSomething is at lines 18-26 in the source above
    affected = analyzer.extract_affected(
        _SPRING_SERVICE_SRC,
        "src/main/java/com/acme/service/CMService.java",
        changed_lines={22},  # inside doSomething body
    )
    assert len(affected) == 1
    assert affected[0].name == "doSomething"
    assert affected[0].kind == "method"
    assert affected[0].qualified_name == (
        "com.acme.service.CMService.doSomething"
    )


def test_analyzer_finds_constructor_when_changed() -> None:
    """Changes inside the constructor should be picked up as 'constructor' kind."""
    # Constructor is at lines 12-15
    affected = analyzer.extract_affected(
        _SPRING_SERVICE_SRC,
        "src/main/java/com/acme/service/CMService.java",
        changed_lines={13, 14},
    )
    assert len(affected) >= 1
    constructors = [a for a in affected if a.kind == "constructor"]
    assert len(constructors) == 1
    assert constructors[0].name == "CMService"


def test_analyzer_returns_empty_when_no_overlap() -> None:
    """Changed lines outside any method should return no affected methods."""
    affected = analyzer.extract_affected(
        _SPRING_SERVICE_SRC,
        "src/main/java/com/acme/service/CMService.java",
        changed_lines={1, 2, 3},  # package/import lines, no methods
    )
    assert affected == []


def test_class_signatures_compact_skips_methods() -> None:
    """Compact mode should include fields + constructor but NOT method
    signatures.
    """
    sigs = analyzer.extract_class_signatures(
        _SPRING_SERVICE_SRC, compact=True
    )
    # Fields present
    assert "private final Daos daos" in sigs
    assert "private final UserService userService" in sigs
    # Constructor present (with body — useful for Claude)
    assert "public CMService(Daos daos" in sigs
    # Method signatures should NOT appear in compact mode
    assert "fetchById" not in sigs
    assert "doSomething" not in sigs
    assert "helper" not in sigs


def test_class_signatures_full_mode_includes_methods() -> None:
    """Non-compact mode should include method signatures."""
    sigs = analyzer.extract_class_signatures(
        _SPRING_SERVICE_SRC, compact=False
    )
    assert "fetchById" in sigs
    assert "doSomething" in sigs


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------


def test_extractor_strips_markdown_fence() -> None:
    """A response wrapped in ``` ```java ... ``` ``` should yield the inner Java."""
    response = """```java
package com.acme.service;

import org.junit.jupiter.api.Test;

class FooTest {
    @Test
    void test1() {}
}
```"""
    result = extractor.extract_java_file(response)
    assert result.startswith("package com.acme.service")
    assert result.rstrip().endswith("}")
    assert "```" not in result


def test_extractor_strips_prose_preamble() -> None:
    """Prose before the package declaration should be discarded."""
    response = """Sure! Here's the test file:

package com.acme.service;

import org.junit.jupiter.api.Test;

class FooTest {
    @Test
    void test1() {}
}"""
    result = extractor.extract_java_file(response)
    assert result.strip().startswith("package com.acme.service")
    assert "Sure!" not in result


def test_extractor_picks_fence_with_package_declaration() -> None:
    """When response has multiple fences, picks the one with ``package``.

    This was the v0.2.0 bug on Kotlin — Claude sometimes wraps a
    source-code snippet (no package) in a fence FIRST, then the real
    file with package SECOND. Extractor must pick the second one.
    """
    response = """Looking at the source code for `doSomething`:

```java
public void doSomething(String input) {
    if (input == null) throw new IllegalArgumentException();
}
```

And here is the fixed test file:

```java
package com.acme.service;

import org.junit.jupiter.api.Test;

class CMServiceTest {
    @Test
    void shouldThrowWhenInputIsNull() {}
}
```"""
    result = extractor.extract_java_file(response)
    assert "package com.acme.service" in result
    assert "class CMServiceTest" in result
    # The source snippet (no package) should NOT be in the extracted output
    assert "public void doSomething" not in result


def test_extractor_raises_when_no_package() -> None:
    """A response with no ``package`` declaration anywhere should raise
    ExtractionError — not silently write garbage to disk.
    """
    response = "Sorry, I cannot generate tests for this code."
    with pytest.raises(extractor.ExtractionError):
        extractor.extract_java_file(response)


def test_extractor_tests_block_finds_at_least_one_test() -> None:
    """Incremental mode extracts ``@Test`` methods from the response."""
    response = """```java
    @Test
    void shouldDoX() {
        // arrange
        when(dao.fetch("x")).thenReturn(null);
        // act
        var result = service.doX();
        // assert
        assertNull(result);
    }

    @Test
    void shouldDoY() {
        assertTrue(true);
    }
```"""
    result = extractor.extract_java_tests_block(response)
    assert "shouldDoX" in result
    assert "shouldDoY" in result
    assert "@Test" in result


# ---------------------------------------------------------------------------
# Handler — path conventions
# ---------------------------------------------------------------------------


def test_handler_suggest_test_path_for_acme_service() -> None:
    """Acme convention: source at src/main/java/com/acme/x/Foo.java
    → test at src/test/java/com/acme/x/FooTest.java (singular Test).
    """
    handler = JavaLanguageHandler()
    test_path = handler.suggest_test_path(
        "src/main/java/com/acme/service/CMService.java"
    )
    assert test_path == "src/test/java/com/acme/service/CMServiceTest.java"


def test_handler_candidate_paths_include_singular_plural_and_IT() -> None:
    """Candidates: Test (Acme), Tests (Spring scaffold), IT (integration)."""
    handler = JavaLanguageHandler()
    cands = handler.candidate_test_paths(
        "src/main/java/com/acme/service/CMService.java"
    )
    assert "src/test/java/com/acme/service/CMServiceTest.java" in cands
    assert "src/test/java/com/acme/service/CMServiceTests.java" in cands
    assert "src/test/java/com/acme/service/CMServiceIT.java" in cands


def test_handler_is_test_file_recognizes_java_tests() -> None:
    handler = JavaLanguageHandler()
    # Files under src/test/
    assert handler.is_test_file("src/test/java/com/foo/BarTest.java")
    # Naming convention alone is also enough
    assert handler.is_test_file("/some/path/FooTest.java")
    assert handler.is_test_file("/some/path/FooTests.java")
    assert handler.is_test_file("/some/path/FooIT.java")
    # Not a test file
    assert not handler.is_test_file("src/main/java/com/foo/Bar.java")
    # Not Java at all
    assert not handler.is_test_file("README.md")


def test_handler_skips_integration_test_paths() -> None:
    handler = JavaLanguageHandler()
    assert handler.is_skipped_test_path("src/test/java/integration/FooIT.java")
    assert handler.is_skipped_test_path("integration/FooTest.java")
    assert not handler.is_skipped_test_path("src/test/java/com/foo/FooTest.java")


# ---------------------------------------------------------------------------
# Runner — build tool detection
# ---------------------------------------------------------------------------


def test_detect_build_tool_finds_maven(tmp_path) -> None:
    (tmp_path / "pom.xml").write_text("<project/>")
    result = runner.detect_build_tool(str(tmp_path))
    assert result is not None
    assert result.tool == "maven"
    assert result.command == ["mvn"]


def test_detect_build_tool_prefers_mvnw_wrapper(tmp_path) -> None:
    (tmp_path / "pom.xml").write_text("<project/>")
    (tmp_path / "mvnw").write_text("#!/bin/sh\n")
    result = runner.detect_build_tool(str(tmp_path))
    assert result is not None
    assert result.command == ["./mvnw"]


def test_detect_build_tool_finds_gradle(tmp_path) -> None:
    (tmp_path / "build.gradle").write_text("// gradle\n")
    result = runner.detect_build_tool(str(tmp_path))
    assert result is not None
    assert result.tool == "gradle"


def test_detect_build_tool_prefers_gradle_when_both_present(tmp_path) -> None:
    """When both pom.xml and build.gradle exist, prefer Gradle. Modern
    Spring Boot leans Gradle, and stray pom.xml files (as in Acme's
    case, where an unrelated ``carbon5`` pom.xml sat next to the real
    ``build.gradle``) are a common source of confusion. The full
    tie-break logic (wrappers) is exercised in
    ``tests/test_java_build_tool_detection.py``.
    """
    (tmp_path / "pom.xml").write_text("<project/>")
    (tmp_path / "build.gradle").write_text("// gradle\n")
    result = runner.detect_build_tool(str(tmp_path))
    assert result is not None
    assert result.tool == "gradle"


def test_detect_build_tool_returns_none_when_neither(tmp_path) -> None:
    result = runner.detect_build_tool(str(tmp_path))
    assert result is None


# ---------------------------------------------------------------------------
# Runner — output parsing
# ---------------------------------------------------------------------------


def test_parse_maven_passing_output() -> None:
    output = """[INFO] Tests run: 5, Failures: 0, Errors: 0, Skipped: 0"""
    result = runner.parse_test_output(output, 0)
    assert result["passed"] == 5
    assert result["failed"] == 0
    assert result["errors"] == 0
    assert result["is_passing"] is True


def test_parse_maven_failing_output() -> None:
    output = """[ERROR] com.acme.service.CMServiceTest.testFoo:42 expected:<1> but was:<2>
[INFO] Tests run: 3, Failures: 1, Errors: 0, Skipped: 0"""
    result = runner.parse_test_output(output, 1)
    assert result["passed"] == 2
    assert result["failed"] == 1
    assert result["is_passing"] is False


def test_parse_gradle_passing_output() -> None:
    output = """> Task :test
SUCCESS: Executed 11 tests in 5s
BUILD SUCCESSFUL in 6s"""
    result = runner.parse_test_output(output, 0)
    assert result["passed"] == 11
    assert result["is_passing"] is True


def test_parse_compile_error_marked_as_errors() -> None:
    output = """[ERROR] COMPILATION ERROR :
[ERROR] CMServiceTest.java:[15,9] cannot find symbol"""
    result = runner.parse_test_output(output, 1)
    assert result["errors"] == 1
    assert result["is_passing"] is False


def test_build_test_command_uses_maven_when_pom_exists(tmp_path) -> None:
    """build_test_command auto-detects Maven and builds -Dtest=Class arg."""
    (tmp_path / "pom.xml").write_text("<project/>")
    cmd = runner.build_test_command(
        ["src/test/java/com/acme/service/CMServiceTest.java"],
        str(tmp_path),
    )
    assert "mvn" in cmd[0]  # could be "mvn" or "./mvnw"
    assert "test" in cmd
    assert any("-Dtest=com.acme.service.CMServiceTest" in c for c in cmd)


def test_build_test_command_uses_gradle_when_only_gradle_present(tmp_path) -> None:
    (tmp_path / "build.gradle").write_text("// gradle")
    cmd = runner.build_test_command(
        ["src/test/java/com/acme/service/CMServiceTest.java"],
        str(tmp_path),
    )
    assert "gradle" in cmd[0]
    assert "test" in cmd
    assert "--tests" in cmd
    assert "com.acme.service.CMServiceTest" in cmd


def test_build_test_command_raises_when_no_build_tool(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="No Java build tool detected"):
        runner.build_test_command(["FooTest.java"], str(tmp_path))


# ---------------------------------------------------------------------------
# Handler — temp file renaming
# ---------------------------------------------------------------------------


def test_temp_file_name_prepends_prefix() -> None:
    handler = JavaLanguageHandler()
    assert handler.temp_test_file_name(
        "src/test/java/com/foo/CMServiceTest.java"
    ) == "_PRBotCMServiceTest.java"


def test_transform_for_temp_file_renames_class() -> None:
    """When writing to a temp file, the class declaration must also
    be renamed so Java's class-name-matches-filename rule is satisfied.
    """
    handler = JavaLanguageHandler()
    original = """package com.acme.service;
class CMServiceTest {
    @Test void foo() {}
}
"""
    transformed = handler.transform_for_temp_file(
        original, "src/test/java/com/acme/service/CMServiceTest.java"
    )
    assert "class _PRBotCMServiceTest" in transformed
    assert "class CMServiceTest " not in transformed  # original gone
