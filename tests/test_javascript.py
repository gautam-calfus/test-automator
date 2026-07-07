"""Tests for the JavaScript/TypeScript language plugin.

Inline JS/TS sources keep these hermetic — no Node.js toolchain is
needed to run them (the runner subprocess is never spawned; command
construction and output parsing are tested against captured shapes).
"""

from __future__ import annotations

import os

import pytest

from test_automator.languages import (
    JavaScriptLanguageHandler,
    all_languages,
    get_handler_by_name,
    get_handler_for_file,
)
from test_automator.languages.base import LanguageHandler
from test_automator.languages.javascript import analyzer, merger, prompts
from test_automator.languages.javascript.extractor import (
    ExtractionError,
    extract_js_file,
    extract_js_tests_block,
    find_matching_paren,
)
from test_automator.models import AffectedFunction, ExistingTest

# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_javascript_is_registered() -> None:
    assert "javascript" in all_languages()


@pytest.mark.parametrize(
    "path",
    [
        "src/utils/format.js",
        "src/utils/format.ts",
        "src/components/App.tsx",
        "src/components/App.jsx",
        "lib/index.mjs",
        "lib/index.cjs",
    ],
)
def test_javascript_resolved_by_extension(path: str) -> None:
    handler = get_handler_for_file(path)
    assert handler is not None
    assert handler.name == "javascript"


def test_javascript_handler_implements_protocol() -> None:
    assert isinstance(JavaScriptLanguageHandler(), LanguageHandler)


def test_other_languages_still_registered() -> None:
    for name in ("python", "kotlin", "java"):
        assert name in all_languages()
        assert get_handler_by_name(name) is not None


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------


_JS_SOURCE = """\
const { toSnake } = require('./case');

function formatName(user) {
  if (!user) {
    return '';
  }
  return `${user.first} ${user.last}`.trim();
}

const percentageOf = (total, value) => {
  if (total === 0) {
    return 0;
  }
  return Math.round((value * 100) / total);
};

class ReportBuilder {
  constructor(rows) {
    this.rows = rows;
  }

  build() {
    return this.rows.map((row) => formatName(row.user));
  }
}

module.exports.summarize = function (rows) {
  return rows.length;
};
"""


def test_analyzer_finds_function_declaration() -> None:
    affected = analyzer.extract_affected(_JS_SOURCE, "src/report.js", {6})
    names = [fn.name for fn in affected]
    assert names == ["formatName"]
    assert affected[0].kind == "function"
    assert affected[0].line_start == 3
    assert "formatName" in affected[0].source_code


def test_analyzer_finds_arrow_function_const() -> None:
    affected = analyzer.extract_affected(_JS_SOURCE, "src/report.js", {12})
    names = [fn.name for fn in affected]
    assert names == ["percentageOf"]
    assert affected[0].kind == "arrow_function"
    # The whole `const percentageOf = ...` statement is the source shown
    assert affected[0].source_code.startswith("const percentageOf")


def test_analyzer_finds_class_method_with_qualified_name() -> None:
    affected = analyzer.extract_affected(_JS_SOURCE, "src/report.js", {23})
    assert [fn.qualified_name for fn in affected] == ["ReportBuilder.build"]
    assert affected[0].kind == "method"


def test_analyzer_finds_module_exports_assignment() -> None:
    affected = analyzer.extract_affected(_JS_SOURCE, "src/report.js", {28})
    assert [fn.name for fn in affected] == ["summarize"]


def test_analyzer_nested_callback_attributed_to_outer_function() -> None:
    # Line 23 is inside the .map() callback — the change belongs to build()
    affected = analyzer.extract_affected(_JS_SOURCE, "src/report.js", {23})
    assert len(affected) == 1
    assert affected[0].name == "build"


def test_analyzer_no_overlap_returns_empty() -> None:
    affected = analyzer.extract_affected(_JS_SOURCE, "src/report.js", {1})
    assert affected == []


_TS_SOURCE = """\
export interface Row {
  id: number;
  label: string;
}

export function pickLabels(rows: Row[]): string[] {
  return rows.map((r) => r.label);
}

export const countRows = (rows: Row[]): number => rows.length;
"""


def test_analyzer_parses_typescript() -> None:
    affected = analyzer.extract_affected(_TS_SOURCE, "src/rows.ts", {7})
    assert [fn.name for fn in affected] == ["pickLabels"]


def test_analyzer_parses_exported_arrow_in_typescript() -> None:
    affected = analyzer.extract_affected(_TS_SOURCE, "src/rows.ts", {10})
    assert [fn.name for fn in affected] == ["countRows"]


def test_analyzer_skips_generated_files() -> None:
    generated = "// @generated\n" + _JS_SOURCE
    assert analyzer.extract_affected(generated, "src/report.js", {6}) == []


def test_analyzer_skips_declaration_files() -> None:
    assert (
        analyzer.extract_affected(_TS_SOURCE, "src/rows.d.ts", {7}) == []
    )


def test_extract_class_signatures_includes_constructor() -> None:
    sigs = analyzer.extract_class_signatures(_JS_SOURCE, "src/report.js")
    assert "class ReportBuilder" in sigs
    assert "constructor(rows)" in sigs
    assert "function formatName(user)" in sigs
    # No bodies
    assert "this.rows = rows" not in sigs


# ---------------------------------------------------------------------------
# Handler: paths and test-file detection
# ---------------------------------------------------------------------------


def test_suggest_test_path_is_colocated_with_same_extension() -> None:
    h = JavaScriptLanguageHandler()
    assert h.suggest_test_path("src/utils/format.ts") == os.path.join(
        "src/utils", "format.test.ts"
    )
    assert h.suggest_test_path("lib/math.js") == os.path.join(
        "lib", "math.test.js"
    )


def test_candidate_test_paths_cover_common_layouts() -> None:
    h = JavaScriptLanguageHandler()
    candidates = h.candidate_test_paths("src/utils/format.ts")
    assert os.path.join("src/utils", "format.test.ts") in candidates
    assert os.path.join("src/utils", "format.spec.ts") in candidates
    assert os.path.join("src/utils", "__tests__", "format.test.ts") in candidates
    assert os.path.join("tests", "utils", "format.test.ts") in candidates


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/utils/format.test.ts", True),
        ("src/utils/format.spec.js", True),
        ("src/utils/__tests__/format.ts", True),
        ("tests/utils/format.ts", True),
        ("src/utils/format.ts", False),
        ("src/latest/format.ts", False),  # "test" substring inside a word
    ],
)
def test_is_test_file(path: str, expected: bool) -> None:
    assert JavaScriptLanguageHandler().is_test_file(path) is expected


def test_temp_test_file_name_still_matches_jest_discovery() -> None:
    h = JavaScriptLanguageHandler()
    name = h.temp_test_file_name("src/utils/format.test.ts")
    assert name == "_prbot.format.test.ts"
    assert name.endswith(".test.ts")


# ---------------------------------------------------------------------------
# Handler: covers()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "fn", "expected"),
    [
        ("camelCase converts keys", "camelCase", True),
        ("camelCase() converts keys", "camelCase", True),
        ("camelCase", "camelCase", True),
        ("camelCaseDeep converts keys", "camelCase", False),
        ("helper uses camelCase inside", "camelCase", False),
        ("", "camelCase", False),
        ("camelCase converts keys", "", False),
    ],
)
def test_covers(title: str, fn: str, expected: bool) -> None:
    assert JavaScriptLanguageHandler().covers(title, fn) is expected


# ---------------------------------------------------------------------------
# Handler: fallback search
# ---------------------------------------------------------------------------


def test_search_finds_test_at_nonconventional_path(tmp_path) -> None:
    (tmp_path / "src" / "utils").mkdir(parents=True)
    (tmp_path / "test" / "utils").mkdir(parents=True)
    (tmp_path / "src" / "utils" / "format.ts").write_text(
        "export const f = () => 1;\n"
    )
    (tmp_path / "test" / "utils" / "format.test.ts").write_text(
        "import { f } from '../../src/utils/format';\n"
        "test('f returns 1', () => { expect(f()).toBe(1); });\n"
    )

    h = JavaScriptLanguageHandler()
    found = h.find_existing_test_file_by_search(
        str(tmp_path), "src/utils/format.ts"
    )
    assert found == os.path.join("test", "utils", "format.test.ts")


def test_search_ignores_same_name_without_import(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "other").mkdir()
    (tmp_path / "src" / "format.ts").write_text("export const f = () => 1;\n")
    # Same filename, but tests an unrelated module
    (tmp_path / "other" / "format.test.ts").write_text(
        "import { g } from './something-else';\n"
        "test('g works', () => {});\n"
    )

    h = JavaScriptLanguageHandler()
    assert (
        h.find_existing_test_file_by_search(str(tmp_path), "src/format.ts")
        is None
    )


def test_search_never_walks_node_modules(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    nm = tmp_path / "node_modules" / "somepkg"
    nm.mkdir(parents=True)
    (tmp_path / "src" / "format.ts").write_text("export const f = () => 1;\n")
    (nm / "format.test.ts").write_text(
        "import { f } from '../../src/format';\ntest('f', () => {});\n"
    )

    h = JavaScriptLanguageHandler()
    assert (
        h.find_existing_test_file_by_search(str(tmp_path), "src/format.ts")
        is None
    )


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------


_FULL_FILE = """\
const { formatName } = require('./report');

describe('formatName', () => {
  test('formatName joins first and last', () => {
    expect(formatName({ first: 'A', last: 'B' })).toBe('A B');
  });
});
"""


def test_extract_js_file_from_fenced_response() -> None:
    raw = f"Here are the tests:\n```javascript\n{_FULL_FILE}```\nLet me know!"
    assert extract_js_file(raw).strip() == _FULL_FILE.strip()


def test_extract_js_file_prefers_test_fence_over_snippet_fence() -> None:
    raw = (
        "First, the function being tested:\n"
        "```js\nfunction formatName(u) {}\n```\n"
        f"And the tests:\n```js\n{_FULL_FILE}```\n"
    )
    assert "describe('formatName'" in extract_js_file(raw)


def test_extract_js_file_unfenced_with_prose() -> None:
    raw = f"Sure — generating now.\n\n{_FULL_FILE}\nThat covers both cases."
    result = extract_js_file(raw)
    assert result.startswith("const { formatName }")
    assert "That covers both cases" not in result


def test_extract_js_file_rejects_pure_prose() -> None:
    with pytest.raises(ExtractionError):
        extract_js_file("I can't generate tests for this file, sorry.")


def test_extract_js_file_rejects_code_without_tests() -> None:
    with pytest.raises(ExtractionError):
        extract_js_file("const x = 1;\nconsole.log(x);\n")


def test_extract_tests_block_two_blocks_with_trailing_prose() -> None:
    raw = """Here are the new tests:

test('percentageOf returns 0 for zero total', () => {
  expect(percentageOf(0, 0)).toBe(0);
});

test('percentageOf rounds to nearest integer', () => {
  expect(percentageOf(3, 1)).toBe(33);
});

These cover the new zero-handling behavior.
"""
    block = extract_js_tests_block(raw)
    assert block.count("test(") == 2
    assert "zero-handling behavior" not in block


def test_extract_tests_block_handles_template_literals() -> None:
    raw = """test('formatName handles template', () => {
  const name = `${user.first} (admin)`;
  expect(formatName(user)).toBe(`${name}!`);
});"""
    block = extract_js_tests_block(raw)
    assert block.strip().endswith("});")


def test_extract_tests_block_handles_test_each() -> None:
    raw = """test.each([
  [0, 0, 0],
  [10, 5, 50],
])('percentageOf(%i, %i) is %i', (total, value, expected) => {
  expect(percentageOf(total, value)).toBe(expected);
});"""
    block = extract_js_tests_block(raw)
    assert "test.each" in block
    assert block.strip().endswith("});")


def test_extract_tests_block_rejects_prose() -> None:
    with pytest.raises(ExtractionError):
        extract_js_tests_block("No new tests are needed here.")


def test_find_matching_paren_skips_strings_and_templates() -> None:
    text = "f('a ) b', `x ${g(')')} y`)"
    assert find_matching_paren(text, 1) == len(text) - 1


# ---------------------------------------------------------------------------
# Merger
# ---------------------------------------------------------------------------


_EXISTING_TESTS = """\
const { percentageOf, formatName } = require('./report');

describe('percentageOf', () => {
  test('percentageOf returns 100 for zero of zero', () => {
    expect(percentageOf(0, 0)).toBe(100);
  });

  it('percentageOf computes simple ratio', () => {
    expect(percentageOf(10, 5)).toBe(50);
  });
});

test('formatName joins names', () => {
  expect(formatName({ first: 'A', last: 'B' })).toBe('A B');
});
"""


def test_parse_existing_tests_finds_nested_and_toplevel() -> None:
    tests = merger.parse_existing_test_functions(_EXISTING_TESTS)
    names = [t.name for t in tests]
    assert names == [
        "percentageOf returns 100 for zero of zero",
        "percentageOf computes simple ratio",
        "formatName joins names",
    ]
    assert tests[0].kind == "test"
    assert tests[1].kind == "it"
    # Line ranges hold the full block
    assert tests[0].line_start == 4
    assert tests[0].line_end == 6


def test_remove_tests_preserves_everything_else() -> None:
    tests = merger.parse_existing_test_functions(_EXISTING_TESTS)
    to_remove = [t for t in tests if t.name.startswith("percentageOf returns")]
    trimmed = merger.remove_tests(_EXISTING_TESTS, to_remove)
    assert "returns 100 for zero of zero" not in trimmed
    assert "computes simple ratio" in trimmed
    assert "formatName joins names" in trimmed
    assert trimmed.startswith("const { percentageOf")


def test_extract_test_source_returns_verbatim_block() -> None:
    tests = merger.parse_existing_test_functions(_EXISTING_TESTS)
    src = merger.extract_test_source(_EXISTING_TESTS, tests[:1])
    assert src.strip().startswith("test('percentageOf returns 100")
    assert src.strip().endswith("});")


def test_merge_new_tests_appends_at_end() -> None:
    new_block = "test('percentageOf returns 0 for zero of zero', () => {\n  expect(percentageOf(0, 0)).toBe(0);\n});"
    merged = merger.merge_new_tests(_EXISTING_TESTS, new_block)
    assert merged.endswith(new_block + "\n")
    assert merged.count("formatName joins names") == 1


def test_merge_empty_new_tests_is_noop() -> None:
    assert merger.merge_new_tests(_EXISTING_TESTS, "  \n") == _EXISTING_TESTS


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def _affected(name: str = "percentageOf") -> AffectedFunction:
    return AffectedFunction(
        file_path="src/report.js",
        name=name,
        qualified_name=name,
        kind="function",
        source_code=f"function {name}(a, b) {{ return a + b; }}",
        line_start=1,
        line_end=1,
        diff_hunk="+  return a + b;",
    )


def test_derive_import_specifier_colocated() -> None:
    assert (
        prompts.derive_import_specifier(
            "src/utils/format.ts", "src/utils/format.test.ts"
        )
        == "./format"
    )


def test_derive_import_specifier_tests_mirror() -> None:
    assert (
        prompts.derive_import_specifier(
            "src/utils/format.ts", "tests/utils/format.test.ts"
        )
        == "../../src/utils/format"
    )


def test_user_prompt_fresh_contains_key_context() -> None:
    h = JavaScriptLanguageHandler()
    prompt = h.user_prompt_fresh("src/report.js", [_affected()])
    assert "src/report.js" in prompt
    assert os.path.join("src", "report.test.js") in prompt
    assert "./report" in prompt
    assert "percentageOf" in prompt
    assert "+  return a + b;" in prompt


def test_user_prompt_incremental_shows_removed_tests() -> None:
    h = JavaScriptLanguageHandler()
    existing = ExistingTest(
        test_file_path="src/report.test.js",
        source_file_path="src/report.js",
        content=_EXISTING_TESTS,
    )
    prompt = h.user_prompt_incremental(
        "src/report.js",
        existing,
        [_affected()],
        trimmed_existing_content="// trimmed",
        removed_tests_code="test('percentageOf old', () => {});",
    )
    assert "percentageOf old" in prompt
    assert "// trimmed" in prompt


def test_system_prompts_encode_title_convention() -> None:
    h = JavaScriptLanguageHandler()
    for sp in (h.system_prompt_fresh(), h.system_prompt_incremental()):
        assert "MUST start with the exact source function name" in sp
    assert "ONLY the complete corrected test file" in h.system_prompt_fix()
