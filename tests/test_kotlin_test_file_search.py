"""Tests for the v0.2.0 filename-based test-file search fallback.

The motivating bug: a user has an existing test file at
``src/test/kotlin/unit/services/UserServiceTests.kt`` (package
``unit.services``) for a source file at
``src/main/kotlin/com/initech/accounts/services/user/UserService.kt``
(package ``com.initech.accounts.services.user``).

The bot's path-derivation convention expects the test at
``src/test/kotlin/unit/services/user/UserServiceTests.kt`` (mirroring
the source package). Before this fix, the bot would not find the
existing test and would create a duplicate at the conventional path,
silently fragmenting the test suite.

After this fix: the bot walks ``src/test/kotlin/`` looking for a file
named ``UserServiceTests.kt``, finds it at the non-conventional path,
verifies it imports the source class, and uses it for incremental
merge.
"""

from __future__ import annotations

import os

import pytest

from test_automator.languages.kotlin.handler import (
    KotlinLanguageHandler,
)


# ---------------------------------------------------------------------------
# The user's actual scenario — file at non-conventional path
# ---------------------------------------------------------------------------


def _make_source_file(repo_root: str) -> str:
    """Create a realistic source file at the Initech conventional
    location and return its relative path.
    """
    src_rel = (
        "src/main/kotlin/com/initech/accounts/services/user/UserService.kt"
    )
    src_full = os.path.join(repo_root, src_rel)
    os.makedirs(os.path.dirname(src_full), exist_ok=True)
    with open(src_full, "w") as fh:
        fh.write(
            "package com.initech.accounts.services.user\n"
            "\n"
            "import org.springframework.stereotype.Service\n"
            "\n"
            "@Service\n"
            "class UserService {\n"
            "    fun foo() = 1\n"
            "}\n"
        )
    return src_rel


def _make_test_file(
    repo_root: str, rel_path: str, imports_source: bool = True
) -> None:
    """Create a test file at the given relative path. If
    ``imports_source`` is True, the file imports the real
    UserService class — making it a valid match for the fallback
    search.
    """
    full = os.path.join(repo_root, rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    pkg = (
        os.path.dirname(rel_path)
        .replace("src/test/kotlin/", "")
        .replace("/", ".")
    )
    import_line = (
        "import com.initech.accounts.services.user.UserService\n"
        if imports_source
        else "import com.something.else.SomeOtherClass\n"
    )
    with open(full, "w") as fh:
        fh.write(
            f"package {pkg}\n"
            "\n"
            f"{import_line}"
            "import org.junit.jupiter.api.Test\n"
            "\n"
            "class UserServiceTests {\n"
            "    @Test\n"
            "    fun `foo works`() {}\n"
            "}\n"
        )


def test_finds_test_at_non_conventional_path(tmp_path) -> None:
    """The user's exact scenario: test file lives one package up from
    where convention says it should. The bot should find it.
    """
    repo = str(tmp_path)
    src_rel = _make_source_file(repo)

    # Test file at non-conventional path (matches user's actual repo)
    _make_test_file(repo, "src/test/kotlin/unit/services/UserServiceTests.kt")

    h = KotlinLanguageHandler()
    found = h.find_existing_test_file_by_search(repo, src_rel)
    assert found == "src/test/kotlin/unit/services/UserServiceTests.kt"


def test_returns_none_when_no_test_file_exists(tmp_path) -> None:
    """If no test file exists ANYWHERE, search returns None and the
    caller falls back to fresh generation at the conventional path.
    """
    repo = str(tmp_path)
    src_rel = _make_source_file(repo)

    h = KotlinLanguageHandler()
    found = h.find_existing_test_file_by_search(repo, src_rel)
    assert found is None


def test_skips_test_file_that_does_not_import_source(tmp_path) -> None:
    """Critical safety check: if a file named UserServiceTests.kt
    exists but doesn't import THIS UserService class (it's a test for
    some other Foo.UserService), the search must NOT match it.

    Without this, we'd attribute tests of unrelated classes to our
    source and accidentally merge unrelated changes into the wrong file.
    """
    repo = str(tmp_path)
    src_rel = _make_source_file(repo)

    # A test file with our expected name but importing a DIFFERENT class
    _make_test_file(
        repo,
        "src/test/kotlin/some/other/path/UserServiceTests.kt",
        imports_source=False,  # imports SomeOtherClass, not UserService
    )

    h = KotlinLanguageHandler()
    found = h.find_existing_test_file_by_search(repo, src_rel)
    assert found is None  # search correctly skips the unrelated file


def test_prefers_conventional_path_when_multiple_match(tmp_path) -> None:
    """If both a conventional-path file and a non-conventional-path
    file exist (and both import the source class), prefer the
    conventional one. The non-conventional one is treated as legacy
    that should eventually be migrated.
    """
    repo = str(tmp_path)
    src_rel = _make_source_file(repo)

    # Both files exist and both import the source
    _make_test_file(repo, "src/test/kotlin/unit/services/UserServiceTests.kt")
    _make_test_file(
        repo, "src/test/kotlin/unit/services/user/UserServiceTests.kt"
    )

    h = KotlinLanguageHandler()
    found = h.find_existing_test_file_by_search(repo, src_rel)
    assert found == "src/test/kotlin/unit/services/user/UserServiceTests.kt"


def test_picks_deterministic_when_multiple_non_conventional_match(
    tmp_path,
) -> None:
    """If multiple non-conventional matches exist and none are at the
    conventional path, picks alphabetically first (deterministic).
    """
    repo = str(tmp_path)
    src_rel = _make_source_file(repo)

    # Two non-conventional files, both import the source class
    _make_test_file(repo, "src/test/kotlin/unit/services/UserServiceTests.kt")
    _make_test_file(repo, "src/test/kotlin/unit/zzzlast/UserServiceTests.kt")

    h = KotlinLanguageHandler()
    found = h.find_existing_test_file_by_search(repo, src_rel)
    # Alphabetically first: services/ before zzzlast/
    assert found == "src/test/kotlin/unit/services/UserServiceTests.kt"


def test_skips_acceptance_directory(tmp_path) -> None:
    """The bot skips ``acceptance/`` subdirs (Cucumber BDD tests).
    Even if a UserServiceTests.kt happens to exist there, search
    should not find it.
    """
    repo = str(tmp_path)
    src_rel = _make_source_file(repo)

    # Put a test file under acceptance/
    _make_test_file(
        repo,
        "src/test/kotlin/acceptance/services/UserServiceTests.kt",
    )

    h = KotlinLanguageHandler()
    found = h.find_existing_test_file_by_search(repo, src_rel)
    assert found is None  # acceptance subdir is skipped


def test_handles_missing_test_directory(tmp_path) -> None:
    """If ``src/test/kotlin/`` doesn't exist at all (very fresh repo
    or non-Kotlin project structure), search returns None without
    crashing.
    """
    repo = str(tmp_path)
    src_rel = _make_source_file(repo)
    # Deliberately don't create src/test/kotlin/

    h = KotlinLanguageHandler()
    found = h.find_existing_test_file_by_search(repo, src_rel)
    assert found is None


def test_handles_source_without_package_declaration(tmp_path) -> None:
    """If we can't read the source file's package declaration (malformed
    file, encoding issues), search returns None — without the FQN we
    can't safely match by filename alone.
    """
    repo = str(tmp_path)
    src_rel = "src/main/kotlin/com/initech/accounts/services/user/UserService.kt"
    src_full = os.path.join(repo, src_rel)
    os.makedirs(os.path.dirname(src_full), exist_ok=True)
    # Write a file with NO package declaration
    with open(src_full, "w") as fh:
        fh.write("// This file has no package line\nclass UserService\n")

    # Make a test file that exists but we can't safely match it without
    # being able to verify the FQN
    _make_test_file(repo, "src/test/kotlin/unit/services/UserServiceTests.kt")

    h = KotlinLanguageHandler()
    found = h.find_existing_test_file_by_search(repo, src_rel)
    assert found is None


def test_accepts_wildcard_import(tmp_path) -> None:
    """Some test files use wildcard imports
    (``import com.initech.accounts.services.user.*``) instead of
    naming the class explicitly. Search must accept this — the
    wildcard pulls in the class.
    """
    repo = str(tmp_path)
    src_rel = _make_source_file(repo)

    # Create a test file with wildcard import of the source package
    full = os.path.join(
        repo, "src/test/kotlin/unit/services/UserServiceTests.kt"
    )
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as fh:
        fh.write(
            "package unit.services\n"
            "\n"
            "import com.initech.accounts.services.user.*\n"
            "import org.junit.jupiter.api.Test\n"
            "\n"
            "class UserServiceTests {}\n"
        )

    h = KotlinLanguageHandler()
    found = h.find_existing_test_file_by_search(repo, src_rel)
    assert found == "src/test/kotlin/unit/services/UserServiceTests.kt"


def test_accepts_aliased_import(tmp_path) -> None:
    """``import com.foo.UserService as Foo`` should also match —
    the file references the source class, just by a different local
    name.
    """
    repo = str(tmp_path)
    src_rel = _make_source_file(repo)

    full = os.path.join(
        repo, "src/test/kotlin/unit/services/UserServiceTests.kt"
    )
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as fh:
        fh.write(
            "package unit.services\n"
            "\n"
            "import com.initech.accounts.services.user.UserService as MyService\n"
            "import org.junit.jupiter.api.Test\n"
            "\n"
            "class UserServiceTests {}\n"
        )

    h = KotlinLanguageHandler()
    found = h.find_existing_test_file_by_search(repo, src_rel)
    assert found == "src/test/kotlin/unit/services/UserServiceTests.kt"


# ---------------------------------------------------------------------------
# TestFinder integration — the fallback fires when conventional fails
# ---------------------------------------------------------------------------


def test_test_finder_uses_search_fallback(tmp_path) -> None:
    """Integration test: TestFinder's _find_test_file should use the
    handler's search fallback when conventional paths don't exist.
    """
    from test_automator.config import LocalTestConfig
    from test_automator.steps.test_finder import TestFinder

    repo = str(tmp_path)
    src_rel = _make_source_file(repo)
    # Test file at non-conventional path
    _make_test_file(repo, "src/test/kotlin/unit/services/UserServiceTests.kt")

    config = LocalTestConfig(
        repo_path=repo, base_branch="main", source_root="src/main/kotlin"
    )
    finder = TestFinder(config)

    # Build a minimal AffectedFunction pointing at our source file
    from test_automator.models import AffectedFunction

    af = AffectedFunction(
        file_path=src_rel,
        name="foo",
        qualified_name="com.initech.accounts.services.user.UserService.foo",
        kind="method",
        source_code="fun foo() = 1",
        line_start=1,
        line_end=1,
    )

    results = finder.find([af])
    assert len(results) == 1
    # Found the file at the non-conventional location
    assert (
        results[0].test_file_path
        == "src/test/kotlin/unit/services/UserServiceTests.kt"
    )


def test_test_finder_prefers_conventional_over_fallback(tmp_path) -> None:
    """When BOTH a conventional and a non-conventional file exist,
    the conventional candidate (which is checked first in phase 1)
    wins. The fallback search shouldn't even fire.
    """
    from test_automator.config import LocalTestConfig
    from test_automator.steps.test_finder import TestFinder

    repo = str(tmp_path)
    src_rel = _make_source_file(repo)
    _make_test_file(repo, "src/test/kotlin/unit/services/UserServiceTests.kt")
    _make_test_file(
        repo, "src/test/kotlin/unit/services/user/UserServiceTests.kt"
    )

    config = LocalTestConfig(
        repo_path=repo, base_branch="main", source_root="src/main/kotlin"
    )
    finder = TestFinder(config)

    from test_automator.models import AffectedFunction

    af = AffectedFunction(
        file_path=src_rel,
        name="foo",
        qualified_name="com.initech.accounts.services.user.UserService.foo",
        kind="method",
        source_code="fun foo() = 1",
        line_start=1,
        line_end=1,
    )

    results = finder.find([af])
    assert len(results) == 1
    # Conventional path wins
    assert (
        results[0].test_file_path
        == "src/test/kotlin/unit/services/user/UserServiceTests.kt"
    )


# ---------------------------------------------------------------------------
# v0.2.1 regression tests — integration/ exclusion + unit/ preference
# ---------------------------------------------------------------------------
#
# Background: in v0.2.0 the search fallback found and modified an
# integration test file because:
#   1. integration/ wasn't in SKIPPED_TEST_SUBDIRS
#   2. The tiebreaker just picked alphabetically first, and
#      integration/ < unit/ alphabetically
#
# Both bugs are fixed in v0.2.1 and exercised below.


def test_skips_integration_directory(tmp_path) -> None:
    """v0.2.1: integration test files must never be touched by the bot.
    Even if a file with the right name and a valid import exists under
    integration/, search must skip it.
    """
    repo = str(tmp_path)
    src_rel = _make_source_file(repo)

    # Test file under integration/ — must be ignored
    _make_test_file(
        repo,
        "src/test/kotlin/integration/services/UserServiceTests.kt",
    )

    h = KotlinLanguageHandler()
    found = h.find_existing_test_file_by_search(repo, src_rel)
    assert found is None  # integration/ is in SKIPPED_TEST_SUBDIRS


def test_prefers_unit_over_integration_when_both_exist(tmp_path) -> None:
    """The exact scenario from the v0.2.0 bug report: both unit/ and
    integration/ have a UserServiceTests.kt that imports the source.
    The bot must pick unit/, NOT integration/ (which would lose to
    integration/ under v0.2.0's alphabetical tiebreaker).
    """
    repo = str(tmp_path)
    src_rel = _make_source_file(repo)

    # Both files exist with valid imports
    _make_test_file(
        repo, "src/test/kotlin/unit/services/UserServiceTests.kt"
    )
    _make_test_file(
        repo,
        "src/test/kotlin/integration/services/UserServiceTests.kt",
    )

    h = KotlinLanguageHandler()
    found = h.find_existing_test_file_by_search(repo, src_rel)
    # integration/ is now in the skip list, so it's not even a candidate.
    # The unit/ match is the only one returned.
    assert found == "src/test/kotlin/unit/services/UserServiceTests.kt"


def test_prefers_unit_over_other_subdir_when_no_conventional(
    tmp_path,
) -> None:
    """If multiple non-conventional matches exist and one is under
    unit/, prefer it over the others. The unit/ subdir is where the bot
    is designed to operate.
    """
    repo = str(tmp_path)
    src_rel = _make_source_file(repo)

    # One under unit/, one under some other non-conventional subdir
    # that ISN'T in the skip list (so both will be found by the search)
    _make_test_file(
        repo, "src/test/kotlin/unit/services/UserServiceTests.kt"
    )
    _make_test_file(
        repo, "src/test/kotlin/aaa/services/UserServiceTests.kt"
    )

    h = KotlinLanguageHandler()
    found = h.find_existing_test_file_by_search(repo, src_rel)
    # Even though "aaa" comes before "unit" alphabetically, unit/
    # wins because that's where unit tests belong.
    assert found == "src/test/kotlin/unit/services/UserServiceTests.kt"


def test_integration_in_path_does_not_skip_unit_test(tmp_path) -> None:
    """Edge case: make sure 'integration' as a substring of a NON-
    integration path doesn't trigger the skip. E.g., a hypothetical
    path containing the word 'integration' in a different context.

    The skip logic should match path segments, not substrings.
    """
    repo = str(tmp_path)
    src_rel = _make_source_file(repo)

    # Create a file at a path that happens to contain "integration"
    # as a substring inside a directory name (not as its own segment)
    # — e.g., "preintegrationhelpers"
    _make_test_file(
        repo,
        "src/test/kotlin/unit/preintegrationhelpers/UserServiceTests.kt",
    )

    h = KotlinLanguageHandler()
    found = h.find_existing_test_file_by_search(repo, src_rel)
    # This file should be found — "preintegrationhelpers" is NOT
    # "integration", even though it contains the substring
    assert (
        found
        == "src/test/kotlin/unit/preintegrationhelpers/UserServiceTests.kt"
    )
