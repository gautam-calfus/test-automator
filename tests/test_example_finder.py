"""Tests for few-shot example selection and the malformed-response
regenerate-once guard.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

from test_automator.steps.example_finder import (
    ExampleFinder,
    format_example_block,
)


class _FakeHandler:
    name = "javascript"
    source_extensions = (".js", ".jsx", ".ts", ".tsx")

    def is_test_file(self, path):
        return path.endswith((".test.js", ".test.jsx", ".spec.js"))

    def candidate_test_paths(self, source_path):
        stem = source_path.rsplit(".", 1)[0]
        return [f"{stem}.test.js"]


def _write(root, rel, content):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(content)


def _finder(root):
    # cache is keyed by (repo, extensions); each tmp_path is unique
    return ExampleFinder(SimpleNamespace(repo_path=root))


def test_prefers_sibling_test_in_same_directory(tmp_path):
    root = str(tmp_path)
    _write(root, "src/Drawer/EditRoleCM.test.js",
           "import { render } from '@testing-library/react';\nrender(<X/>);")
    _write(root, "src/Other/Far.test.js", "test('x', () => {});")

    ex = _finder(root).find_example(
        _FakeHandler(), "src/Drawer/EditRoleExpert.js", exclude_paths=set()
    )
    assert ex is not None
    assert ex[0].replace("\\", "/") == "src/Drawer/EditRoleCM.test.js"


def test_prefers_harness_example_for_components(tmp_path):
    root = str(tmp_path)
    # Same directory distance; only one shows the RTL harness.
    _write(root, "src/a/plain.test.js", "test('adds', () => { expect(1).toBe(1); });")
    _write(root, "src/a/withharness.test.js",
           "import { render, screen } from '@testing-library/react';\n"
           "import { Provider } from 'react-redux';\nrender(<C/>);")

    ex = _finder(root).find_example(
        _FakeHandler(), "src/a/Component.jsx", exclude_paths=set()
    )
    assert ex is not None and "withharness" in ex[0]


def test_excludes_targets_own_test_file(tmp_path):
    root = str(tmp_path)
    _write(root, "src/a/Foo.test.js", "render(<Foo/>);")  # would be self
    ex = _finder(root).find_example(
        _FakeHandler(), "src/a/Foo.js",
        exclude_paths={"src/a/Foo.test.js"},
    )
    assert ex is None  # only candidate was excluded


def test_returns_none_when_no_tests_exist(tmp_path):
    root = str(tmp_path)
    _write(root, "src/a/Foo.js", "export const x = 1;")
    assert _finder(root).find_example(
        _FakeHandler(), "src/a/Foo.js", exclude_paths=set()
    ) is None


def test_format_block_empty_when_no_example():
    assert format_example_block(None) == ""


def test_format_block_includes_content_and_path():
    block = format_example_block(("src/a/Foo.test.js", "render(<Foo/>);"))
    assert "src/a/Foo.test.js" in block
    assert "render(<Foo/>);" in block
    assert "MIRROR its setup" in block


# --- regenerate-once on malformed extraction ---

from test_automator.config import LocalTestConfig  # noqa: E402
from test_automator.models import AffectedFunction  # noqa: E402
from test_automator.languages.python.handler import (  # noqa: E402
    PythonLanguageHandler,
)
from test_automator.steps.test_finder import TestFinder  # noqa: E402
from test_automator.steps.test_generator import TestGenerator  # noqa: E402
from test_automator.utils.exceptions import TestGeneratorError  # noqa: E402
import pytest  # noqa: E402


class _FlakyLLM:
    """Serves pre-queued responses; records how many calls happened."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def generate(self, system_prompt, user_prompt):
        self.calls += 1
        return self._responses.pop(0)


def _fn():
    return AffectedFunction(
        file_path="mod.py", name="foo", qualified_name="foo",
        kind="function", source_code="def foo():\n    return 1",
        line_start=1, line_end=2,
    )


def _gen(tmp_path, llm):
    cfg = LocalTestConfig(repo_path=str(tmp_path), base_branch="main")
    return TestGenerator(cfg, TestFinder(cfg), llm)


def test_regenerates_once_on_unusable_response(tmp_path):
    llm = _FlakyLLM([
        "e(true);\n });   garbage, no importable code",
        "def test_foo():\n    assert True\n",
    ])
    result = _gen(tmp_path, llm)._generate_fresh(
        PythonLanguageHandler(), "mod.py", [_fn()]
    )
    assert llm.calls == 2  # retried once
    assert "def test_foo" in result.content


def test_gives_up_after_two_bad_responses(tmp_path):
    llm = _FlakyLLM(["garbage one @@@", "garbage two @@@"])
    with pytest.raises(TestGeneratorError):
        _gen(tmp_path, llm)._generate_fresh(
            PythonLanguageHandler(), "mod.py", [_fn()]
        )
    assert llm.calls == 2  # two attempts then give up, not infinite
