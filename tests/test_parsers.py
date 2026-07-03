"""Smoke tests for the diff parser and test-module parser."""

from test_automator.utils.diff_parser import (
    extract_code_block,
    parse_changed_lines,
)
from test_automator.utils.test_parser import (
    covers,
    parse_test_functions,
)


def test_parse_changed_lines_basic() -> None:
    patch = (
        "@@ -1,3 +1,4 @@\n"
        " line1\n"
        " line2\n"
        "+new_line\n"
        " line3\n"
    )
    assert parse_changed_lines(patch) == {3}


def test_extract_code_block_with_fences() -> None:
    text = "Here:\n```python\nprint('hi')\n```\nDone."
    assert extract_code_block(text) == "print('hi')"


def test_extract_code_block_fallback() -> None:
    assert extract_code_block("no fences") == "no fences"


def test_parse_test_functions_basic() -> None:
    content = """
import pytest

def helper():
    pass

@pytest.mark.unit
def test_foo():
    pass

def test_bar():
    pass
"""
    fns = parse_test_functions(content)
    names = [f.name for f in fns]
    assert names == ["test_foo", "test_bar"]


def test_covers_exact() -> None:
    assert covers("test_apply_discount", "apply_discount")


def test_covers_prefix() -> None:
    assert covers("test_apply_discount_zero", "apply_discount")


def test_covers_no_match() -> None:
    assert not covers("test_other", "apply_discount")
