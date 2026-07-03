"""Tests for v0.3.0a3: build tool detection prefers Gradle when both
build files are present.

Real user scenario: Acme is a Spring Boot Gradle project (build.gradle,
Java 11, all real dependencies). But the repo also has a stray pom.xml
from an unrelated carbon5 subproject (JUnit 3.8.1, no Java version set,
which triggers Maven's default source=5 that no longer compiles on
modern JDKs).

Old behavior: preferred Maven when both existed → ran with the wrong
pom.xml → compile failed with "Source option 5 is no longer supported"
before any generated tests could run.

New behavior: prefers Gradle when both build files present, with a
wrapper-based tie-break (whichever has its wrapper wins).
"""

from __future__ import annotations

import os

import pytest

from test_automator.languages.java.runner import (
    BuildToolDetection,
    detect_build_tool,
)


def _touch(path: str) -> None:
    """Create an empty file, creating parent dirs as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    open(path, "w").close()


def test_both_present_prefers_gradle_when_only_gradle_has_wrapper(tmp_path) -> None:
    """When both pom.xml and build.gradle exist but only ./gradlew is
    present, that's a strong signal that Gradle is the real build tool.
    This is EXACTLY Acme's setup."""
    repo = str(tmp_path)
    _touch(os.path.join(repo, "pom.xml"))
    _touch(os.path.join(repo, "build.gradle"))
    _touch(os.path.join(repo, "gradlew"))

    result = detect_build_tool(repo)

    assert result is not None
    assert result.tool == "gradle"
    assert result.command == ["./gradlew"]


def test_both_present_prefers_maven_when_only_mvnw_has_wrapper(tmp_path) -> None:
    """Symmetric case: Maven has wrapper, Gradle doesn't → prefer Maven."""
    repo = str(tmp_path)
    _touch(os.path.join(repo, "pom.xml"))
    _touch(os.path.join(repo, "build.gradle"))
    _touch(os.path.join(repo, "mvnw"))

    result = detect_build_tool(repo)

    assert result is not None
    assert result.tool == "maven"
    assert result.command == ["./mvnw"]


def test_both_present_neither_wrapper_defaults_to_gradle(tmp_path) -> None:
    """If we can't distinguish, prefer Gradle. Modern Spring Boot projects
    lean Gradle, and stray pom.xml files are a more common source of
    confusion than stray build.gradle files.
    """
    repo = str(tmp_path)
    _touch(os.path.join(repo, "pom.xml"))
    _touch(os.path.join(repo, "build.gradle"))

    result = detect_build_tool(repo)

    assert result is not None
    assert result.tool == "gradle"
    assert result.command == ["gradle"]


def test_both_present_both_wrappers_still_prefers_gradle(tmp_path) -> None:
    """When both wrappers exist too, Gradle wins."""
    repo = str(tmp_path)
    _touch(os.path.join(repo, "pom.xml"))
    _touch(os.path.join(repo, "build.gradle"))
    _touch(os.path.join(repo, "mvnw"))
    _touch(os.path.join(repo, "gradlew"))

    result = detect_build_tool(repo)

    assert result is not None
    assert result.tool == "gradle"
    assert result.command == ["./gradlew"]


def test_only_pom_uses_maven(tmp_path) -> None:
    """Pure Maven project — no ambiguity."""
    repo = str(tmp_path)
    _touch(os.path.join(repo, "pom.xml"))

    result = detect_build_tool(repo)
    assert result is not None
    assert result.tool == "maven"


def test_only_gradle_uses_gradle(tmp_path) -> None:
    """Pure Gradle project — no ambiguity."""
    repo = str(tmp_path)
    _touch(os.path.join(repo, "build.gradle"))

    result = detect_build_tool(repo)
    assert result is not None
    assert result.tool == "gradle"


def test_only_gradle_kts_uses_gradle(tmp_path) -> None:
    """Kotlin DSL Gradle also counts."""
    repo = str(tmp_path)
    _touch(os.path.join(repo, "build.gradle.kts"))

    result = detect_build_tool(repo)
    assert result is not None
    assert result.tool == "gradle"


def test_neither_returns_none(tmp_path) -> None:
    """No build tool found → None (caller decides how to handle)."""
    result = detect_build_tool(str(tmp_path))
    assert result is None
