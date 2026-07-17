"""Tests for uv-aware pytest invocation in the Python runner.

The runner runs the target project's pytest through ``uv run`` when the
project is uv-managed, so tests execute against the deps in ``uv.lock``
instead of whatever interpreter is on PATH. These tests pin the
detection + command-construction rules for the auto/uv/pip preferences.
"""

from __future__ import annotations

import pytest

from test_automator.languages.python import runner
from test_automator.languages.python.handler import PythonLanguageHandler


def _write(path, text=""):
    path.write_text(text, encoding="utf-8")


# --- detection -------------------------------------------------------------

def test_uses_uv_true_with_uv_lock(tmp_path):
    _write(tmp_path / "uv.lock")
    assert runner._uses_uv(str(tmp_path)) is True


def test_uses_uv_true_with_tool_uv_table(tmp_path):
    _write(
        tmp_path / "pyproject.toml",
        "[project]\nname = 'x'\n\n[tool.uv]\ndev-dependencies = []\n",
    )
    assert runner._uses_uv(str(tmp_path)) is True


def test_uses_uv_true_with_nested_tool_uv_table(tmp_path):
    _write(
        tmp_path / "pyproject.toml",
        "[project]\nname = 'x'\n\n[tool.uv.sources]\nfoo = {}\n",
    )
    assert runner._uses_uv(str(tmp_path)) is True


def test_uses_uv_false_for_plain_pip_project(tmp_path):
    _write(
        tmp_path / "pyproject.toml",
        "[project]\nname = 'x'\ndependencies = ['requests']\n",
    )
    assert runner._uses_uv(str(tmp_path)) is False


def test_uses_uv_false_when_no_markers(tmp_path):
    assert runner._uses_uv(str(tmp_path)) is False


# --- command construction --------------------------------------------------

def test_pip_preference_always_plain_python(tmp_path):
    _write(tmp_path / "uv.lock")  # uv-managed, but pip is forced
    cmd = runner.build_test_command(["t.py"], str(tmp_path), "pip")
    assert cmd[:3] == ["python", "-m", "pytest"]
    assert "uv" not in cmd


def test_uv_preference_always_uv_run(tmp_path):
    # Forced uv, even with no uv markers: surface a clear error if uv is
    # missing rather than silently using a different interpreter.
    cmd = runner.build_test_command(["t.py"], str(tmp_path), "uv")
    assert cmd[:5] == ["uv", "run", "python", "-m", "pytest"]


def test_auto_uses_uv_when_managed_and_available(tmp_path, monkeypatch):
    _write(tmp_path / "uv.lock")
    monkeypatch.setattr(runner, "_uv_available", lambda: True)
    cmd = runner.build_test_command(["t.py"], str(tmp_path), "auto")
    assert cmd[:5] == ["uv", "run", "python", "-m", "pytest"]


def test_auto_falls_back_when_uv_not_installed(tmp_path, monkeypatch):
    _write(tmp_path / "uv.lock")
    monkeypatch.setattr(runner, "_uv_available", lambda: False)
    cmd = runner.build_test_command(["t.py"], str(tmp_path), "auto")
    assert cmd[:3] == ["python", "-m", "pytest"]
    assert "uv" not in cmd


def test_auto_plain_for_non_uv_project(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_uv_available", lambda: True)
    cmd = runner.build_test_command(["t.py"], str(tmp_path), "auto")
    assert cmd[:3] == ["python", "-m", "pytest"]


def test_default_preference_is_auto(tmp_path, monkeypatch):
    # build_test_command defaults python_runner to "auto".
    monkeypatch.setattr(runner, "_uv_available", lambda: True)
    _write(tmp_path / "uv.lock")
    cmd = runner.build_test_command(["t.py"], str(tmp_path))
    assert cmd[:2] == ["uv", "run"]


def test_pytest_flags_preserved_under_uv(tmp_path):
    cmd = runner.build_test_command(["a.py", "b.py"], str(tmp_path), "uv")
    # The plugin/cache/verbosity flags and the test files still ride along.
    for flag in ("--tb=short", "--no-header", "-v", "-o", "addopts=",
                 "-p", "no:cacheprovider", "a.py", "b.py"):
        assert flag in cmd


# --- handler plumbing ------------------------------------------------------

def test_handler_defaults_to_auto(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_uv_available", lambda: True)
    _write(tmp_path / "uv.lock")
    h = PythonLanguageHandler()
    cmd = h.build_test_command(["t.py"], str(tmp_path))
    assert cmd[:2] == ["uv", "run"]


def test_handler_set_python_runner_pip(tmp_path):
    _write(tmp_path / "uv.lock")
    h = PythonLanguageHandler()
    h.set_python_runner("pip")
    cmd = h.build_test_command(["t.py"], str(tmp_path))
    assert cmd[:3] == ["python", "-m", "pytest"]


def test_handler_set_python_runner_none_resets_to_auto(tmp_path):
    h = PythonLanguageHandler()
    h.set_python_runner(None)  # type: ignore[arg-type]
    assert h._python_runner == "auto"
