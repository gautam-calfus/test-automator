"""Tests for the JavaScript (Jest/Vitest) runner module.

Output fixtures mirror real Jest 29 / Vitest 1.x output shapes. No Node
toolchain is required — command construction and parsing only.
"""

from __future__ import annotations

import json

from test_automator.languages.javascript import runner

# ---------------------------------------------------------------------------
# Framework detection
# ---------------------------------------------------------------------------


def _write_pkg(tmp_path, payload: dict) -> str:
    (tmp_path / "package.json").write_text(json.dumps(payload))
    return str(tmp_path)


def test_detect_defaults_to_jest_without_package_json(tmp_path) -> None:
    assert runner.detect_framework(str(tmp_path)) == "jest"


def test_detect_jest_from_dev_dependencies(tmp_path) -> None:
    repo = _write_pkg(tmp_path, {"devDependencies": {"jest": "^29.0.0"}})
    assert runner.detect_framework(repo) == "jest"


def test_detect_vitest_from_dev_dependencies(tmp_path) -> None:
    repo = _write_pkg(tmp_path, {"devDependencies": {"vitest": "^1.6.0"}})
    assert runner.detect_framework(repo) == "vitest"


def test_test_script_outranks_dependencies(tmp_path) -> None:
    """Mid-migration repo: both frameworks installed, but the team runs
    vitest — the script is the source of truth.
    """
    repo = _write_pkg(
        tmp_path,
        {
            "scripts": {"test": "vitest run"},
            "devDependencies": {"jest": "^29.0.0", "vitest": "^1.6.0"},
        },
    )
    assert runner.detect_framework(repo) == "vitest"


def test_build_command_jest_uses_exact_paths(tmp_path) -> None:
    repo = _write_pkg(tmp_path, {"devDependencies": {"jest": "^29.0.0"}})
    cmd = runner.build_test_command(["src/_prbot.a.test.js"], repo)
    assert cmd[:3] == ["npx", "--no-install", "jest"]
    assert "--runTestsByPath" in cmd
    assert "--json" in cmd
    assert cmd[-1] == "src/_prbot.a.test.js"


def test_build_command_vitest(tmp_path) -> None:
    repo = _write_pkg(tmp_path, {"devDependencies": {"vitest": "^1.6.0"}})
    cmd = runner.build_test_command(["src/_prbot.a.test.ts"], repo)
    assert cmd[:4] == ["npx", "--no-install", "vitest", "run"]
    assert "--reporter=json" in cmd


# ---------------------------------------------------------------------------
# Output parsing — JSON path
# ---------------------------------------------------------------------------


def _jest_json(passed: int, failed: int, failed_names=()) -> str:
    return json.dumps(
        {
            "numTotalTestSuites": 1,
            "numTotalTests": passed + failed,
            "numPassedTests": passed,
            "numFailedTests": failed,
            "numRuntimeErrorTestSuites": 0,
            "success": failed == 0,
            "testResults": [
                {
                    "assertionResults": [
                        *(
                            {
                                "status": "failed",
                                "fullName": name,
                                "title": name,
                            }
                            for name in failed_names
                        ),
                        {
                            "status": "passed",
                            "fullName": "x passes",
                            "title": "x passes",
                        },
                    ]
                }
            ],
        }
    )


def test_parse_passing_jest_json() -> None:
    output = "Determining test suites to run...\n" + _jest_json(52, 0)
    result = runner.parse_test_output(output, return_code=0)
    assert result["passed"] == 52
    assert result["failed"] == 0
    assert result["errors"] == 0
    assert result["is_passing"] is True


def test_parse_failing_jest_json_collects_failed_ids() -> None:
    output = _jest_json(51, 1, failed_names=["percentageOf returns 0"])
    result = runner.parse_test_output(output, return_code=1)
    assert result["passed"] == 51
    assert result["failed"] == 1
    assert result["failed_test_ids"] == ["percentageOf returns 0"]
    assert result["is_passing"] is False


def test_parse_json_with_surrounding_console_noise() -> None:
    output = (
        "console.log\n    some app log line\n\n"
        + _jest_json(3, 0)
        + "\nDone in 2.41s.\n"
    )
    result = runner.parse_test_output(output, return_code=0)
    assert result["passed"] == 3
    assert result["is_passing"] is True


# ---------------------------------------------------------------------------
# Output parsing — human summary fallback
# ---------------------------------------------------------------------------


def test_parse_jest_human_summary() -> None:
    output = """\
 PASS  src/report.test.js
 FAIL  src/other.test.js

Tests:       2 failed, 1 skipped, 49 passed, 52 total
Snapshots:   0 total
Time:        3.2 s
"""
    result = runner.parse_test_output(output, return_code=1)
    assert result["passed"] == 49
    assert result["failed"] == 2
    assert result["is_passing"] is False


def test_parse_vitest_human_summary() -> None:
    output = """\
 ✓ src/report.test.ts (52)

 Test Files  1 passed (1)
      Tests  1 failed | 51 passed (52)
   Start at  09:12:40
   Duration  1.24s
"""
    result = runner.parse_test_output(output, return_code=1)
    assert result["passed"] == 51
    assert result["failed"] == 1
    assert result["is_passing"] is False


def test_parse_passing_human_summary_requires_rc_zero() -> None:
    output = "Tests:       52 passed, 52 total\n"
    assert runner.parse_test_output(output, 0)["is_passing"] is True
    assert runner.parse_test_output(output, 1)["is_passing"] is False


# ---------------------------------------------------------------------------
# Output parsing — startup/environment errors
# ---------------------------------------------------------------------------


def test_no_summary_and_nonzero_rc_counts_one_error() -> None:
    output = "npm error could not determine executable to run\n"
    result = runner.parse_test_output(output, return_code=1)
    assert result["errors"] == 1
    assert result["passed"] == 0
    assert result["is_passing"] is False


def test_collection_error_markers_are_environment_only() -> None:
    markers = runner.collection_error_markers()
    # Environment errors: bail the fix loop
    assert "npm error" in markers
    assert "jest: not found" in markers
    assert "Cannot find module 'jest'" in markers
    # Fixable-by-rewriting errors must NOT bail the fix loop (the
    # v0.2.0a6.post4 lesson): a bad relative import in a generated
    # test produces "Cannot find module './foo'" — that exact generic
    # form must not be a marker.
    assert "Cannot find module" not in markers
    assert "SyntaxError" not in markers
