"""Tests for formatting-only change detection.

A formatter reblocking a region makes git mark every line changed, so
the analyzer would flag every function in it. only_formatting_changed
lets us drop functions whose behavior is identical — while NEVER
dropping a real change.
"""

from __future__ import annotations

from test_automator.utils.semantic_diff import (
    normalize,
    only_formatting_changed,
)


def test_prettier_style_reformat_is_formatting_only():
    base = (
        "export const getTags=(data)=>{\n"
        "  return (dispatch)=>{\n"
        "    dispatch({\n"
        "      type:'getkeytags',\n"
        "      payload:data\n"
        "    })\n"
        "  }\n"
        "}"
    )
    current = (
        "export const getTags = (data) => {\n"
        "  return (dispatch) => {\n"
        "    dispatch({\n"
        '      type: "getkeytags",\n'
        "      payload: data,\n"
        "    });\n"
        "  };\n"
        "};"
    )
    assert only_formatting_changed(base, current) is True


def test_real_logic_change_is_not_formatting_only():
    base = "const f = (x) => x + 1;"
    current = "const f = (x) => x - 1;"
    assert only_formatting_changed(base, current) is False


def test_changed_string_literal_is_a_real_change():
    base = "dispatch({ type: 'A', payload: data })"
    current = "dispatch({ type: 'B', payload: data })"
    assert only_formatting_changed(base, current) is False


def test_word_boundary_is_preserved():
    # 'return x' must never normalize equal to 'returnx'
    assert normalize("return x") != normalize("returnx")


def test_added_statement_is_a_real_change():
    base = "function g() {\n  a();\n}"
    current = "function g() {\n  a();\n  b();\n}"
    assert only_formatting_changed(base, current) is False


def test_python_reindent_that_changes_flow_is_not_formatting_only():
    # b() moves OUT of the if block — semantic change; indent matters.
    base = "def f(x):\n    if x:\n        a()\n        b()"
    current = "def f(x):\n    if x:\n        a()\n    b()"
    assert only_formatting_changed(base, current, indent_significant=True) is False


def test_python_pure_whitespace_is_formatting_only():
    base = "def f(x):\n    return x+1"
    current = "def f(x):\n    return x + 1"
    assert only_formatting_changed(base, current, indent_significant=True) is True


def test_empty_inputs_are_not_formatting_only():
    assert only_formatting_changed("", "x") is False
    assert only_formatting_changed("x", "") is False
