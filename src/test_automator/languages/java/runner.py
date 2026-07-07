"""Java test runner — Maven + Gradle support with auto-detect.

Auto-detects build tool from the repository:
- ``pom.xml``                  → Maven (prefer ``./mvnw`` over ``mvn``)
- ``build.gradle`` / ``.kts``  → Gradle (prefer ``./gradlew`` over ``gradle``)
- both present → Gradle (tie-break: whichever has its wrapper)

Output parsing:
- Maven Surefire:   ``Tests run: N, Failures: M, Errors: E, Skipped: S``
- Gradle test-logger: ``SUCCESS/FAILURE: Executed N tests``

The signatures match the LanguageHandler protocol:
- ``build_test_command(test_files, repo_path) -> list[str]``
- ``parse_test_output(output, return_code) -> dict[...]``
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass


@dataclass(frozen=True)
class BuildToolDetection:
    """Detected build tool for a project."""
    tool: str  # "maven" or "gradle"
    command: list[str]  # base command (e.g. ["./mvnw"] or ["mvn"])


def detect_build_tool(repo_path: str) -> BuildToolDetection | None:
    """Detect Maven vs Gradle. Returns None if neither is found.

    When BOTH pom.xml and build.gradle[.kts] are present (a surprisingly
    common situation with stray/orphaned build files), we can't know
    just by presence which is the real build tool. The heuristic:

    1. **Prefer whichever has its wrapper script.** If ./mvnw exists,
       Maven is the real one. If ./gradlew exists, Gradle is. If both
       have wrappers, prefer Gradle (modern Spring Boot leans Gradle).
       If neither has a wrapper, prefer Gradle for the same reason.

    2. **If only one build file exists,** use it. Wrappers preferred
       over system binaries when available (wrappers pin the version,
       matters for reproducibility).

    v0.3.0a3: previously preferred Maven when both existed. This
    caused Acme's stray ``pom.xml`` (for an unrelated ``carbon5``
    subproject, JUnit 3.8.1, source=5) to be selected over the real
    ``build.gradle`` (Spring Boot 2.3, sourceCompatibility=11), which
    then failed with "Source option 5 is no longer supported".
    """
    pom = os.path.join(repo_path, "pom.xml")
    gradle = os.path.join(repo_path, "build.gradle")
    gradle_kts = os.path.join(repo_path, "build.gradle.kts")
    mvnw = os.path.join(repo_path, "mvnw")
    gradlew = os.path.join(repo_path, "gradlew")

    has_pom = os.path.isfile(pom)
    has_gradle = os.path.isfile(gradle) or os.path.isfile(gradle_kts)
    has_mvnw = os.path.isfile(mvnw)
    has_gradlew = os.path.isfile(gradlew)

    if has_pom and has_gradle:
        # Both build files present — tie-break by wrapper, then default
        # to Gradle. This matches Acme's actual setup: real Gradle
        # build + stray Maven pom that would silently break things.
        if has_gradlew and not has_mvnw:
            return BuildToolDetection(tool="gradle", command=["./gradlew"])
        if has_mvnw and not has_gradlew:
            return BuildToolDetection(tool="maven", command=["./mvnw"])
        # Both have wrappers OR neither has a wrapper — prefer Gradle
        if has_gradlew:
            return BuildToolDetection(tool="gradle", command=["./gradlew"])
        return BuildToolDetection(tool="gradle", command=["gradle"])

    if has_pom:
        if has_mvnw:
            return BuildToolDetection(tool="maven", command=["./mvnw"])
        return BuildToolDetection(tool="maven", command=["mvn"])

    if has_gradle:
        if has_gradlew:
            return BuildToolDetection(tool="gradle", command=["./gradlew"])
        return BuildToolDetection(tool="gradle", command=["gradle"])

    return None


def _path_to_class_name(test_file_path: str) -> str:
    """Convert a test file path to its fully-qualified class name.

    Example: ``src/test/java/com/acme/service/CMServiceTest.java``
    → ``com.acme.service.CMServiceTest``
    """
    norm = test_file_path.replace("\\", "/")
    marker = "src/test/java/"
    idx = norm.find(marker)
    if idx == -1:
        # Fallback: just use the file stem
        base = os.path.basename(norm)
        stem, _ = os.path.splitext(base)
        return stem
    rest = norm[idx + len(marker):]
    no_ext = os.path.splitext(rest)[0]
    return no_ext.replace("/", ".")


def build_test_command(test_files: list[str], repo_path: str) -> list[str]:
    """Construct the subprocess argv to run the given test files.

    Auto-detects Maven or Gradle. For Maven, passes ``-Dtest=Class1,Class2``;
    for Gradle, passes ``--tests Class1 --tests Class2``.

    Raises RuntimeError if no build tool can be detected — the
    orchestrator surfaces this as a failed step rather than running
    something nonsensical.
    """
    detected = detect_build_tool(repo_path)
    if detected is None:
        raise RuntimeError(
            "No Java build tool detected. Expected to find pom.xml "
            "(Maven) or build.gradle[.kts] (Gradle) at the repo root: "
            f"{repo_path}"
        )

    class_names = [_path_to_class_name(p) for p in test_files]

    if detected.tool == "maven":
        # -Dtest accepts comma-separated class names
        return detected.command + [
            "test",
            f"-Dtest={','.join(class_names)}",
            "-Dsurefire.failIfNoTests=false",
            "-q",
        ]

    # Gradle
    cmd = detected.command + ["test", "--console=plain"]
    for cn in class_names:
        cmd.extend(["--tests", cn])
    return cmd


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

# Maven Surefire: "Tests run: 5, Failures: 1, Errors: 0, Skipped: 0"
# May appear multiple times (per test class + final aggregate). Take the LAST.
_MVN_SUMMARY_RE = re.compile(
    r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)",
    re.IGNORECASE,
)

# Maven Surefire per-test failure line:
#   "[ERROR] com.foo.BarTest.testBaz:42 expected:<1> but was:<2>"
_MVN_FAILED_TEST_RE = re.compile(
    r"^\[ERROR\]\s+([\w.$]+Test(?:s)?)(?:[.:#])(\w+)",
    re.MULTILINE,
)

# Gradle test-logger: "SUCCESS: Executed 11 tests" / "FAILURE: Executed 11 tests in 5s (1 failed)"
_GRADLE_SUCCESS_RE = re.compile(
    r"^SUCCESS:\s+Executed\s+(\d+)\s+tests?", re.MULTILINE
)
_GRADLE_FAILURE_RE = re.compile(
    r"^FAILURE:\s+Executed\s+(\d+)\s+tests?\s+in\s+\S+\s+\((\d+)\s+failed\)",
    re.MULTILINE,
)
_GRADLE_FAILED_LINE_RE = re.compile(
    r"^\s+Test\s+(.+?)\(\)\s+FAILED",
    re.MULTILINE,
)

# Compile error markers — when these appear, tests never ran
_COMPILE_ERROR_MARKERS = (
    "COMPILATION ERROR",         # Maven
    "Failed to execute goal",    # Maven plugin failures
    "[ERROR] BUILD FAILURE",     # Maven
    "Task :compileJava FAILED",  # Gradle
    "Task :compileTestJava FAILED",  # Gradle
)


def _is_compile_error(output: str) -> bool:
    return any(m in output for m in _COMPILE_ERROR_MARKERS)


def parse_test_output(
    output: str, return_code: int
) -> dict[str, int | bool | list[str]]:
    """Parse runner output into the protocol's standard dict shape.

    Returns: passed, failed, errors, failed_test_ids, is_passing.

    Detection order:
    1. Maven Surefire summary → counts come from there
    2. Gradle test-logger summary → counts come from there
    3. Neither + compile error markers → errors=1
    4. Neither + nonzero return code → errors=1
    5. Otherwise → all zeros (probably no tests matched)
    """
    passed = 0
    failed = 0
    errors = 0
    failed_test_ids: list[str] = []

    mvn_matches = list(_MVN_SUMMARY_RE.finditer(output))
    gradle_success = _GRADLE_SUCCESS_RE.search(output)
    gradle_failure = _GRADLE_FAILURE_RE.search(output)

    if mvn_matches:
        # Maven: take the LAST summary line (aggregate for multi-module
        # or final summary at end of run)
        last = mvn_matches[-1]
        run = int(last.group(1))
        failed = int(last.group(2))
        errors_part = int(last.group(3))
        # treat Maven "Errors" as additional failures from the bot's POV
        # (the orchestrator triggers the fix loop on either failed or
        # errored tests)
        passed = max(0, run - failed - errors_part)
        # Maven "Errors" count maps to our "errors"
        errors = errors_part
        # Extract failed test IDs from [ERROR] lines
        for m in _MVN_FAILED_TEST_RE.finditer(output):
            failed_test_ids.append(f"{m.group(1)}.{m.group(2)}")
    elif gradle_failure:
        total = int(gradle_failure.group(1))
        failed = int(gradle_failure.group(2))
        passed = max(total - failed, 0)
        failed_test_ids = _GRADLE_FAILED_LINE_RE.findall(output)
    elif gradle_success:
        passed = int(gradle_success.group(1))
    else:
        # No summary parsed. Either compile error or no-tests-ran.
        if _is_compile_error(output) or return_code != 0:
            errors = 1

    is_passing = (
        return_code == 0
        and (mvn_matches or gradle_success is not None)
        and failed == 0
        and errors == 0
    )

    return {
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "failed_test_ids": failed_test_ids,
        "is_passing": bool(is_passing),
    }


def parse_test_results_xml(
    repo_root: str, class_names: set[str]
) -> dict[str, int | bool | list[str]] | None:
    """Parse JUnit XML result files as a fallback when the console
    output has no test summary (v0.3.0a10).

    Real Acme failure this fixes: plain ``./gradlew test`` prints NO
    per-test summary when everything passes — the ``SUCCESS: Executed N
    tests`` line the console parser looks for comes from the third-party
    test-logger plugin, which Acme doesn't apply. So a fully GREEN
    run (13/13 passing) parsed as 0/0/0, the pipeline declared failure,
    and the fix loop burned Claude calls rewriting passing tests.

    Gradle and Maven both always write JUnit XML (``build/test-results/
    test/TEST-<class>.xml`` and ``target/surefire-reports/``). We read
    only the files for ``class_names`` (the classes we just ran) so
    stale results from other suites can't pollute the counts.

    Returns None when no matching XML exists or it reports zero tests —
    the caller keeps the console-parse result in that case.
    """
    result_dirs = (
        os.path.join(repo_root, "build", "test-results", "test"),
        os.path.join(repo_root, "target", "surefire-reports"),
    )

    passed = 0
    failed = 0
    errors = 0
    failed_test_ids: list[str] = []
    found_any = False

    for results_dir in result_dirs:
        if not os.path.isdir(results_dir):
            continue
        for name in sorted(os.listdir(results_dir)):
            if not (name.startswith("TEST-") and name.endswith(".xml")):
                continue
            cls = name[len("TEST-"):-len(".xml")]
            if cls not in class_names:
                continue
            try:
                root = ET.parse(os.path.join(results_dir, name)).getroot()
            except (ET.ParseError, OSError):
                continue
            suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
            for suite in suites:
                found_any = True
                total = int(suite.get("tests", 0))
                sfailed = int(suite.get("failures", 0))
                serrors = int(suite.get("errors", 0))
                skipped = int(suite.get("skipped", 0))
                passed += max(0, total - sfailed - serrors - skipped)
                failed += sfailed
                errors += serrors
                for case in suite.iter("testcase"):
                    if case.find("failure") is not None or case.find("error") is not None:
                        failed_test_ids.append(
                            f"{case.get('classname')}.{case.get('name')}"
                        )

    if not found_any or (passed + failed + errors) == 0:
        return None

    return {
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "failed_test_ids": failed_test_ids,
        "is_passing": failed == 0 and errors == 0,
    }


def collection_error_markers() -> tuple[str, ...]:
    """Substrings indicating an ENVIRONMENT error (vs a fixable compile
    error). When these appear, the fix loop bails — Claude can't fix
    missing dependencies by rewriting test code.
    """
    return (
        # Maven dependency resolution / plugin issues
        "Could not resolve dependencies for project",
        "Could not transfer artifact",
        "Plugin execution not covered by lifecycle configuration",
        "Failed to read artifact descriptor",
        # Gradle dependency resolution
        "Could not resolve all files for configuration",
        "Could not find or load main class",
        # JVM / daemon issues (shared with Kotlin)
        "Could not create service of type FileAccessTimeJournal",
        "Timeout waiting to lock journal cache",
        "Could not start your build",
        # JDK/Gradle version mismatch — e.g. Gradle 6.x run under JDK 17
        # ("major version 61"). Nothing test-related ever runs; the fix
        # is pointing JAVA_HOME at a JDK the pinned Gradle supports.
        "Unsupported class file major version",
        "Could not determine java version from",
        "Could not compile settings file",
    )
