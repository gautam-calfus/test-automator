"""Tests for v0.3.0a6: --file whitelist.

User can point at ONE (or a few) specific files instead of getting
the full diff processed. Trumps --java-file-filter.
"""

from __future__ import annotations

from test_automator.config import LocalTestConfig
from test_automator.models import AffectedFunction
from test_automator.orchestrator import LocalTestPipeline


def _fn(path: str) -> AffectedFunction:
    return AffectedFunction(
        file_path=path,
        name="x",
        qualified_name=f"{path}.x",
        kind="method_declaration",
        source_code="void x() {}",
        line_start=1,
        line_end=1,
        diff_hunk="+ x",
        class_context="",
    )


def _make_orchestrator(**config_kwargs):
    config = LocalTestConfig(
        repo_path="/tmp", base_branch="main", **config_kwargs
    )
    # Directly instantiate without wiring up the real dependencies —
    # we only exercise the private methods here.
    orch = LocalTestPipeline.__new__(LocalTestPipeline)
    orch._config = config
    return orch


def test_file_whitelist_drops_non_whitelisted():
    """Only the file in --file passes through."""
    orch = _make_orchestrator(
        file_whitelist=["src/main/java/com/acme/service/CMService.java"]
    )
    affected = [
        _fn("src/main/java/com/acme/service/CMService.java"),
        _fn("src/main/java/com/acme/service/EmailService.java"),
        _fn("src/main/java/com/acme/idp/OktaEventHookController.java"),
    ]
    result = orch._apply_file_whitelist(affected)
    assert len(result) == 1
    assert result[0].file_path == "src/main/java/com/acme/service/CMService.java"


def test_file_whitelist_supports_multiple():
    """Multiple --file flags whitelist multiple files."""
    orch = _make_orchestrator(
        file_whitelist=[
            "src/main/java/com/acme/service/CMService.java",
            "src/main/java/com/acme/idp/OktaEventHookController.java",
        ]
    )
    affected = [
        _fn("src/main/java/com/acme/service/CMService.java"),
        _fn("src/main/java/com/acme/service/EmailService.java"),
        _fn("src/main/java/com/acme/idp/OktaEventHookController.java"),
    ]
    result = orch._apply_file_whitelist(affected)
    assert len(result) == 2
    paths = {f.file_path for f in result}
    assert "src/main/java/com/acme/service/CMService.java" in paths
    assert "src/main/java/com/acme/idp/OktaEventHookController.java" in paths


def test_no_whitelist_lets_everything_through():
    """When --file isn't passed (whitelist is None), all files pass."""
    orch = _make_orchestrator(file_whitelist=None)
    affected = [
        _fn("src/main/java/com/acme/service/CMService.java"),
        _fn("src/main/java/com/acme/service/EmailService.java"),
    ]
    result = orch._apply_file_whitelist(affected)
    assert len(result) == 2


def test_path_normalization_handles_leading_dot_slash():
    """User might type ``./src/main/java/Foo.java`` — should match."""
    orch = _make_orchestrator(
        file_whitelist=["./src/main/java/com/acme/service/CMService.java"]
    )
    affected = [
        _fn("src/main/java/com/acme/service/CMService.java"),
    ]
    result = orch._apply_file_whitelist(affected)
    assert len(result) == 1


def test_path_normalization_handles_backslashes():
    """Windows-style paths should also match."""
    orch = _make_orchestrator(
        file_whitelist=["src\\main\\java\\com\\acme\\service\\CMService.java"]
    )
    affected = [
        _fn("src/main/java/com/acme/service/CMService.java"),
    ]
    result = orch._apply_file_whitelist(affected)
    assert len(result) == 1
