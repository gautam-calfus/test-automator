"""Tests for the concise one-line runner-failure summary.

When a run produces no parseable test summary (tests errored before
running — a render/import/syntax error), the log should read like a
diagnosis, not dump a scary multi-frame stack trace.
"""

from __future__ import annotations

from test_automator.steps.test_runner import _summarize_runner_failure


def test_react_undefined_property_is_diagnosed():
    out = (
        "console.error\n    Error: Uncaught [TypeError: Cannot read "
        "properties of undefined (reading 'expertTopics')]\n"
        "        at reportException (.../jsdom/...)\n"
        "        at beginWork$1 (.../react-dom.development.js:27451:7)\n"
    )
    msg = _summarize_runner_failure(out)
    assert "expertTopics" in msg
    assert "undefined" in msg
    assert "fix loop" in msg
    # one line, not a stack dump
    assert "\n" not in msg
    assert "react-dom" not in msg


def test_not_a_function():
    msg = _summarize_runner_failure("TypeError: dispatch is not a function")
    assert "dispatch" in msg and "\n" not in msg


def test_missing_module():
    msg = _summarize_runner_failure("Cannot find module './api' from 'x.js'")
    assert "./api" in msg and "resolve" in msg


def test_syntax_error():
    msg = _summarize_runner_failure("SyntaxError: Unexpected token '}'")
    assert "syntax error" in msg.lower()


def test_missing_jsdom_is_flagged_as_setup_not_fixable():
    msg = _summarize_runner_failure(
        "Test environment jest-environment-jsdom cannot be found"
    )
    assert "jest-environment-jsdom" in msg
    assert "project setup" in msg


def test_generic_fallback_is_short_and_single_line():
    msg = _summarize_runner_failure("noise\nmore noise\nnothing useful here")
    assert "\n" not in msg
    assert "no parseable summary" in msg


def test_first_error_line_fallback():
    out = "ok line\nRuntimeError: something specific broke\nmore"
    msg = _summarize_runner_failure(out)
    assert "something specific broke" in msg
    assert "\n" not in msg
