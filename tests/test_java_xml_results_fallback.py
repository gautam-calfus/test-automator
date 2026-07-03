"""Tests for v0.3.0a10: JUnit XML fallback for Java test results.

Real Acme run (Gautam, July 2026): 13 generated tests all PASSED,
but plain ``./gradlew test`` prints no per-test summary on success —
the ``SUCCESS: Executed N tests`` line the console parser expects comes
from the third-party test-logger plugin, which Acme doesn't apply.
The run parsed as 0/0/0, the pipeline declared FAIL, and the fix loop
burned two Claude calls rewriting tests that were already green.

Fallback: when the console yields nothing (exit 0, no summary, no
compile error), read Gradle's JUnit XML reports for exactly the classes
that were run.
"""

from __future__ import annotations

import os
import textwrap

from pr_test_automator_local.languages.java import runner
from pr_test_automator_local.languages.java.handler import JavaLanguageHandler


_PASSING_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.acme.idp._PRBotUserDeactivationServiceTest" tests="13" skipped="0" failures="0" errors="0" time="0.783">
  <testcase name="shouldReturnDeactivatedWhenUserIsActive()" classname="com.acme.idp._PRBotUserDeactivationServiceTest" time="0.002"/>
</testsuite>
"""

_FAILING_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.acme.idp._PRBotFooTest" tests="3" skipped="0" failures="1" errors="0" time="0.1">
  <testcase name="ok()" classname="com.acme.idp._PRBotFooTest" time="0.001"/>
  <testcase name="alsoOk()" classname="com.acme.idp._PRBotFooTest" time="0.001"/>
  <testcase name="broken()" classname="com.acme.idp._PRBotFooTest" time="0.001">
    <failure message="expected 1 but was 2" type="org.opentest4j.AssertionFailedError">stack</failure>
  </testcase>
</testsuite>
"""

# Gradle's console output for a fully passing run WITHOUT test-logger:
# no per-test summary at all.
_SILENT_GRADLE_OUTPUT = """\
> Task :compileTestJava
> Task :test

BUILD SUCCESSFUL in 5s
5 actionable tasks: 2 executed, 3 up-to-date
"""


def _write_xml(root: str, class_name: str, content: str) -> None:
    d = os.path.join(root, "build", "test-results", "test")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"TEST-{class_name}.xml"), "w") as fh:
        fh.write(content)


def test_xml_parse_counts_passing_suite(tmp_path):
    root = str(tmp_path)
    cls = "com.acme.idp._PRBotUserDeactivationServiceTest"
    _write_xml(root, cls, _PASSING_XML)

    result = runner.parse_test_results_xml(root, {cls})

    assert result is not None
    assert result["passed"] == 13
    assert result["failed"] == 0
    assert result["errors"] == 0
    assert result["is_passing"] is True


def test_xml_parse_extracts_failed_test_ids(tmp_path):
    root = str(tmp_path)
    cls = "com.acme.idp._PRBotFooTest"
    _write_xml(root, cls, _FAILING_XML)

    result = runner.parse_test_results_xml(root, {cls})

    assert result is not None
    assert result["passed"] == 2
    assert result["failed"] == 1
    assert result["is_passing"] is False
    assert result["failed_test_ids"] == [
        "com.acme.idp._PRBotFooTest.broken()"
    ]


def test_xml_parse_ignores_other_classes(tmp_path):
    """Stale XML from a different suite must not pollute the counts."""
    root = str(tmp_path)
    _write_xml(root, "com.acme.idp._PRBotStaleTest", _FAILING_XML)

    result = runner.parse_test_results_xml(
        root, {"com.acme.idp._PRBotUserDeactivationServiceTest"}
    )

    assert result is None


def test_xml_parse_returns_none_without_results_dir(tmp_path):
    result = runner.parse_test_results_xml(str(tmp_path), {"com.x.FooTest"})
    assert result is None


def test_handler_falls_back_to_xml_on_silent_gradle_output(tmp_path):
    """End-to-end handler behavior on the exact Acme scenario:
    silent console + green XML → is_passing=True."""
    root = str(tmp_path)
    # detect_build_tool needs a build file + wrapper
    open(os.path.join(root, "build.gradle"), "w").write("// gradle\n")
    open(os.path.join(root, "gradlew"), "w").write("#!/bin/sh\n")

    cls = "com.acme.idp._PRBotUserDeactivationServiceTest"
    _write_xml(root, cls, _PASSING_XML)

    handler = JavaLanguageHandler()
    handler.build_test_command(
        ["src/test/java/com/acme/idp/_PRBotUserDeactivationServiceTest.java"],
        root,
    )
    result = handler.parse_test_output(_SILENT_GRADLE_OUTPUT, 0)

    assert result["passed"] == 13
    assert result["is_passing"] is True


def test_handler_does_not_use_xml_when_console_has_summary(tmp_path):
    """When the test-logger summary IS present, the console result wins
    (no surprise behavior change for projects that have the plugin)."""
    root = str(tmp_path)
    open(os.path.join(root, "build.gradle"), "w").write("// gradle\n")
    open(os.path.join(root, "gradlew"), "w").write("#!/bin/sh\n")

    cls = "com.acme.idp._PRBotUserDeactivationServiceTest"
    _write_xml(root, cls, _PASSING_XML)

    handler = JavaLanguageHandler()
    handler.build_test_command(
        ["src/test/java/com/acme/idp/_PRBotUserDeactivationServiceTest.java"],
        root,
    )
    result = handler.parse_test_output("SUCCESS: Executed 4 tests\n", 0)

    assert result["passed"] == 4


def test_handler_does_not_use_xml_on_compile_error(tmp_path):
    """A compile error means the tests never ran — stale green XML from
    a previous run must NOT mask it."""
    root = str(tmp_path)
    open(os.path.join(root, "build.gradle"), "w").write("// gradle\n")
    open(os.path.join(root, "gradlew"), "w").write("#!/bin/sh\n")

    cls = "com.acme.idp._PRBotUserDeactivationServiceTest"
    _write_xml(root, cls, _PASSING_XML)

    handler = JavaLanguageHandler()
    handler.build_test_command(
        ["src/test/java/com/acme/idp/_PRBotUserDeactivationServiceTest.java"],
        root,
    )
    output = "> Task :compileTestJava FAILED\nBUILD FAILED in 2s\n"
    result = handler.parse_test_output(output, 1)

    assert result["errors"] == 1
    assert result["is_passing"] is False
