"""JavaScript/TypeScript (Jest / Vitest) test runner.

Builds the ``npx`` invocation and parses the output. Framework is
auto-detected from the project's ``package.json`` — the ``scripts.test``
command is checked first (it's what the team actually runs), then the
dependency lists; Jest is the default.

Both frameworks are asked for machine-readable JSON output
(``jest --json`` / ``vitest --reporter=json`` — Vitest's JSON schema is
deliberately Jest-compatible: numTotalTests, numPassedTests,
testResults[].assertionResults[]...). The parser prefers that JSON and
falls back to the human summary lines if it's absent:

    Jest:   Tests:       1 failed, 51 passed, 52 total
    Vitest:  Tests  1 failed | 51 passed (52)

Command notes:
- ``npx --no-install`` never downloads packages implicitly — if the
  project doesn't have the framework installed, we want a loud,
  classifiable error, not a surprise network install.
- ``jest --runTestsByPath`` treats args as exact file paths instead of
  regex patterns, so temp-file names never fail discovery filters.
- ``--ci`` stops Jest writing new snapshots during our ephemeral runs.
"""

from __future__ import annotations

import json
import os
import re

# ---------------------------------------------------------------------------
# Build the command
# ---------------------------------------------------------------------------


def detect_framework(repo_path: str) -> str:
    """Return "jest", "vitest", or "react-scripts" based on package.json.

    Signal priority:
    1. The ``scripts.test`` command — it's what the team actually runs,
       so it settles mid-migration repos that have both frameworks in
       devDependencies. ``react-scripts test`` (Create React App) is
       checked first: CRA embeds its Jest config inside react-scripts,
       so invoking bare ``jest`` there fails with a config error.
    2. Which framework appears in dependencies/devDependencies
       (react-scripts implies CRA; Jest wins the remaining tie).
    3. Default to "jest" when package.json is missing or unreadable.
    """
    pkg_path = os.path.join(repo_path, "package.json")
    try:
        with open(pkg_path, encoding="utf-8") as fh:
            pkg = json.load(fh)
    except (OSError, ValueError):
        return "jest"

    script = ""
    scripts = pkg.get("scripts")
    if isinstance(scripts, dict):
        script = str(scripts.get("test", ""))
    if "react-scripts" in script:
        return "react-scripts"
    if "vitest" in script:
        return "vitest"
    if "jest" in script:
        return "jest"

    deps: dict = {}
    for key in ("dependencies", "devDependencies"):
        section = pkg.get(key)
        if isinstance(section, dict):
            deps.update(section)

    if "react-scripts" in deps:
        return "react-scripts"
    if "jest" in deps:
        return "jest"
    if "vitest" in deps:
        return "vitest"
    return "jest"


def build_test_command(test_files: list[str], repo_path: str) -> list[str]:
    framework = detect_framework(repo_path)
    if framework == "vitest":
        return [
            "npx", "--no-install", "vitest", "run",
            "--reporter=json",
            *test_files,
        ]
    if framework == "react-scripts":
        # CRA: react-scripts test wraps Jest and forwards unknown args
        # to it. --watchAll=false is essential — without it the runner
        # enters interactive watch mode and hangs until the subprocess
        # timeout kills it.
        return [
            "npx", "--no-install", "react-scripts", "test",
            "--watchAll=false",
            "--ci", "--json",
            "--runTestsByPath",
            *test_files,
        ]
    return [
        "npx", "--no-install", "jest",
        "--ci", "--json",
        "--runTestsByPath",
        *test_files,
    ]


# ---------------------------------------------------------------------------
# Parse the output
# ---------------------------------------------------------------------------

# Jest human summary: "Tests:       1 failed, 2 skipped, 51 passed, 54 total"
_JEST_SUMMARY_RE = re.compile(r"^Tests:\s+(?P<parts>.+)$", re.MULTILINE)
_JEST_PART_RE = re.compile(r"(\d+)\s+(failed|passed|skipped|todo|total)")

# Vitest human summary: " Tests  1 failed | 51 passed (52)"
_VITEST_SUMMARY_RE = re.compile(
    r"^\s*Tests\s+(?:(?P<failed>\d+)\s+failed\s*\|\s*)?"
    r"(?P<passed>\d+)\s+passed",
    re.MULTILINE,
)


def parse_test_output(
    output: str, return_code: int
) -> dict[str, int | bool | list[str]]:
    """Convert Jest/Vitest output into structured counts.

    Preference order:
    1. The JSON result object (contains ``numTotalTests``) — exact
       counts, per-test failure ids, and the framework's own
       ``success`` verdict.
    2. The human summary line.
    3. Neither summary present: with a non-zero return code that's an
       environment/startup error — count it as one error so the
       orchestrator surfaces a failure (mirrors the Kotlin runner).
    """
    data = _find_json_result(output)
    if data is not None:
        passed = int(data.get("numPassedTests", 0))
        failed = int(data.get("numFailedTests", 0))
        errors = int(data.get("numRuntimeErrorTestSuites", 0))
        failed_ids = _failed_ids_from_json(data)
        is_passing = (
            return_code == 0
            and bool(data.get("success", failed == 0 and errors == 0))
        )
        return {
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "failed_test_ids": failed_ids,
            "is_passing": is_passing,
        }

    passed = failed = errors = 0
    found_summary = False

    jest_match = _JEST_SUMMARY_RE.search(output)
    if jest_match:
        found_summary = True
        for count, kind in _JEST_PART_RE.findall(jest_match.group("parts")):
            if kind == "passed":
                passed = int(count)
            elif kind == "failed":
                failed = int(count)
    else:
        vitest_match = _VITEST_SUMMARY_RE.search(output)
        if vitest_match:
            found_summary = True
            passed = int(vitest_match.group("passed"))
            failed = int(vitest_match.group("failed") or 0)

    if not found_summary and return_code != 0:
        errors = 1

    is_passing = (
        return_code == 0
        and found_summary
        and failed == 0
        and passed > 0
    )
    return {
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "failed_test_ids": [],
        "is_passing": is_passing,
    }


def _find_json_result(output: str) -> dict | None:
    """Locate and decode the framework's JSON result object in mixed
    output (human-readable lines go to stderr but we capture combined).

    The object is identified by its ``numTotalTests`` key. We look for
    a ``{`` at the start of a line before that key and use
    ``raw_decode`` so trailing non-JSON text doesn't break decoding.
    """
    marker = output.find('"numTotalTests"')
    if marker == -1:
        return None

    decoder = json.JSONDecoder()
    # Candidate object starts: every line-leading "{" before the marker,
    # nearest first (the top-level object is printed on its own line).
    starts = [
        m.start() for m in re.finditer(r"^\{", output[:marker], re.MULTILINE)
    ]
    if output.startswith("{"):
        starts.insert(0, 0)
    for start in reversed(starts):
        try:
            data, _ = decoder.raw_decode(output[start:])
        except ValueError:
            continue
        if isinstance(data, dict) and "numTotalTests" in data:
            return data
    return None


def _failed_ids_from_json(data: dict) -> list[str]:
    ids: list[str] = []
    for suite in data.get("testResults", []) or []:
        for assertion in suite.get("assertionResults", []) or []:
            if assertion.get("status") == "failed":
                ids.append(
                    assertion.get("fullName")
                    or assertion.get("title")
                    or "(unnamed test)"
                )
    return ids


def collection_error_markers() -> tuple[str, ...]:
    """Substrings in runner output that signal a BUILD ENVIRONMENT
    error (NOT a failure in the generated test code).

    Same philosophy as the Kotlin runner (v0.2.0a6.post4 lesson):
    ordinary compile/type/module-resolution errors INSIDE the generated
    test are deliberately NOT listed — the fix loop can often repair
    those by rewriting the test (wrong relative import, ESM/CJS syntax
    mismatch). Only errors the LLM cannot fix by editing test code
    belong here.
    """
    return (
        # Toolchain missing entirely
        "npx: command not found",
        "npm: command not found",
        # npm couldn't resolve/run the framework binary
        # (npx --no-install prints these when jest/vitest isn't installed)
        "npm error",
        "npm ERR!",
        "jest: not found",
        "vitest: not found",
        "react-scripts: not found",
        "could not determine executable to run",
        # jsdom test environment missing — project setup, not test content
        "Cannot find module 'jsdom'",
        # Jest configuration problems — project setup, not test content
        "● Validation Error:",  # "● Validation Error:"
        "Test environment jest-environment",
        # Framework itself not installed as a module
        "Cannot find module 'jest'",
        "Cannot find module 'vitest'",
    )
