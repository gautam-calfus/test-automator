"""Tests for read-only repo access during generation (the fix for
tests that compile but fail on wrong assumptions).

Background — the failure that motivated this. A generated test verified
``cmAlertScheduleDao.deactivateActive(...)`` for a PRIVATE method that
is only reachable through a 150-line public entry point guarded by
``expiredQuestionAlertDTO != null && isUserACm`` plus a validation
helper with early returns. Mockito reported "Wanted but not invoked …
there were zero interactions with this mock", and three fix attempts
failed to converge.

The root cause was not the model: prompts omit method bodies over
~2000 chars (and fall back to signatures-only for files over 30K), and
the bridge passed ``--tools ""`` so the model could not read the file
to recover what was omitted. It had to guess the control flow it was
supposed to satisfy.

Fix: the model gets Read/Grep/Glob (never Write/Edit/Bash) with the
repo as its working directory, and the prompts point at exact file
paths and line ranges. Writing stays entirely with the pipeline.
"""

from __future__ import annotations

import subprocess

import pytest

from test_automator.llm_bridge import (
    ClaudeCodeBridge,
    CopilotCliBridge,
    create_bridge,
)
from test_automator.models import AffectedFunction


@pytest.fixture
def captured(monkeypatch):
    record: dict = {}

    def fake_run(cmd, **kwargs):
        record["argv"] = cmd
        record.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return record


def test_file_access_grants_read_only_tools(captured):
    bridge = ClaudeCodeBridge(cmd="echo", timeout=5, workdir="/repo")

    bridge.generate("sys", "user")

    argv = captured["argv"]
    tools_at = argv.index("--tools")
    granted = argv[tools_at + 1:tools_at + 4]
    assert granted == ["Read", "Grep", "Glob"]


def test_file_access_never_grants_write_tools(captured):
    """The pipeline owns the working tree — the model must not be able
    to write, edit, or run commands."""
    bridge = ClaudeCodeBridge(cmd="echo", timeout=5, workdir="/repo")

    bridge.generate("sys", "user")

    argv = captured["argv"]
    for forbidden in ("Write", "Edit", "Bash", "NotebookEdit"):
        assert forbidden not in argv
    assert "--allow-dangerously-skip-permissions" not in argv


def test_file_access_adds_repo_dir(captured):
    bridge = ClaudeCodeBridge(cmd="echo", timeout=5, workdir="/repo")

    bridge.generate("sys", "user")

    argv = captured["argv"]
    assert argv[argv.index("--add-dir") + 1] == "/repo"


def test_cli_runs_inside_the_repo(captured):
    """Relative paths in prompts must resolve, so the subprocess cwd is
    the repo root."""
    bridge = ClaudeCodeBridge(cmd="echo", timeout=5, workdir="/repo")

    bridge.generate("sys", "user")

    assert captured["cwd"] == "/repo"


def test_file_access_can_be_disabled(captured):
    """--no-llm-file-access reverts to the old blind one-shot mode."""
    bridge = ClaudeCodeBridge(
        cmd="echo", timeout=5, workdir="/repo", file_access=False
    )

    bridge.generate("sys", "user")

    argv = captured["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert "Read" not in argv
    assert "--add-dir" not in argv


def test_factory_forwards_workdir_and_file_access(captured):
    bridge = create_bridge(
        "claude", cmd="echo", workdir="/repo", file_access=True
    )
    bridge.generate("sys", "user")
    assert captured["cwd"] == "/repo"
    assert "Read" in captured["argv"]


def test_factory_forwards_workdir_to_other_providers(captured):
    bridge = create_bridge("copilot", cmd="echo", workdir="/repo")
    bridge.generate("sys", "user")
    assert captured["cwd"] == "/repo"


def test_other_providers_still_get_no_tool_permissions(captured):
    bridge = CopilotCliBridge(cmd="echo", timeout=5, workdir="/repo")
    bridge.generate("sys", "user")
    assert "--allow-all-tools" not in captured["argv"]


# ---------------------------------------------------------------------------
# Prompt side: omitted bodies must be locatable
# ---------------------------------------------------------------------------


def _big_method(diff_hunk: str) -> AffectedFunction:
    return AffectedFunction(
        file_path="src/main/java/com/acme/service/AdminService.java",
        name="configureUserGeoRoutingAtrs",
        qualified_name="AdminService.configureUserGeoRoutingAtrs",
        kind="method",
        source_code="public void configureUserGeoRoutingAtrs() {\n"
        + "    // filler\n" * 400 + "}",
        line_start=560,
        line_end=712,
        diff_hunk=diff_hunk,
    )


def test_omitted_body_points_at_file_and_lines():
    """When the body is too large to inline, the prompt must say where
    to read it — that body holds the guards the test has to satisfy."""
    from test_automator.languages.java.prompts import (
        _render_functions_for_prompt,
    )

    rendered = _render_functions_for_prompt([_big_method("+ one line")])

    assert "body omitted" in rendered
    assert "READ THE REAL BODY" in rendered
    assert "src/main/java/com/acme/service/AdminService.java" in rendered
    assert "lines 560-712" in rendered


def test_inlined_body_still_reports_its_location():
    from test_automator.languages.java.prompts import (
        _render_functions_for_prompt,
    )

    small = AffectedFunction(
        file_path="src/main/java/com/acme/service/AdminService.java",
        name="shouldSend",
        qualified_name="AdminService.shouldSend",
        kind="method",
        source_code="boolean shouldSend() { return true; }",
        line_start=42,
        line_end=44,
    )

    rendered = _render_functions_for_prompt([small])

    assert "AdminService.java:42" in rendered
    assert "return true" in rendered


def test_system_prompts_instruct_reading_the_source():
    """All three modes must tell the model to verify against the real
    source; otherwise granting the tools changes nothing."""
    from test_automator.languages.java import prompts

    for prompt in (
        prompts.SYSTEM_PROMPT_FRESH,
        prompts.SYSTEM_PROMPT_INCREMENTAL,
        prompts.SYSTEM_PROMPT_FIX,
    ):
        assert "Read" in prompt and "Grep" in prompt
    # The fix prompt must specifically address the zero-interactions
    # failure rather than inviting the model to delete the assertion.
    # Normalize whitespace — the prompt text is hard-wrapped.
    fix_flat = " ".join(prompts.SYSTEM_PROMPT_FIX.lower().split())
    assert "zero interactions" in fix_flat
    assert "strict" in prompts.SYSTEM_PROMPT_FRESH.lower()
