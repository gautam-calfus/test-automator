"""LLM prompts for JavaScript/TypeScript (Jest/Vitest) test generation.

Three prompt pairs (system + user): fresh generation, incremental merge,
and failure fix — same structure as the Python and Kotlin prompt modules.

Design decisions encoded here:

- Test titles MUST start with the exact source function name followed
  by a space (``camelCase converts snake_case keys``). This is not
  style pedantry: ``handler.covers()`` uses that prefix to know which
  existing tests to replace when a function changes again. A test the
  bot can't attribute is a test it can never update.

- Module system is MIRRORED, never chosen: the user prompt shows the
  source file's own imports and the prompt requires matching them
  (``require`` ↔ ``require``, ``import`` ↔ ``import``). Guessing wrong
  is the #1 way generated JS tests fail to even load (ESM/CJS mismatch).

- Incremental responses contain ONLY new test blocks with NO imports.
  The merger appends blocks at file end; a stray import there would be
  valid JS but style-noise, and a duplicate import would be a crash in
  ESM. If a new module is genuinely needed, ``require()`` inside the
  test body is the escape hatch (works in both CJS and, via
  interop, most ts-jest setups).
"""

from __future__ import annotations

import os

from test_automator.models import (
    AffectedFunction,
    ExistingTest,
    GeneratedTest,
)

# ============================================================================
# System prompts
# ============================================================================

SYSTEM_PROMPT_FRESH = """\
You are an expert JavaScript/TypeScript test engineer generating unit
tests for a Node.js project. The project's test framework is Jest (or
Vitest with a Jest-compatible API — the same test code works for both).

Generate a COMPLETE new test file for the changed functions you are
given.

== Required style ==

MODULE SYSTEM:
- Mirror the source file's import style EXACTLY. If the source uses
  `require(...)`, write `const { fn } = require('<module>')`. If it
  uses `import`, write `import { fn } from '<module>'`.
- Import the functions under test from the module path given in the
  user prompt. Do not guess other paths.
- If the source file is TypeScript, the test file is TypeScript: type
  annotations are allowed but keep them minimal.

TEST STRUCTURE:
- One `describe('<functionName>', () => { ... })` block per source
  function.
- Every test title MUST start with the exact source function name
  followed by a space, e.g.:
      test('camelCase converts snake_case keys to camelCase', ...)
  This naming is mandatory — tooling uses the prefix to track which
  tests cover which function.
- Use `test(...)` (not `it(...)`) for new tests.
- Cover the normal path, edge cases visible in the code (empty input,
  null/undefined where the signature allows it), and error paths
  (`expect(() => fn(bad)).toThrow(...)` / `await expect(p).rejects`).

MOCKING:
- Mock external modules with `jest.mock('<module>')` at the top of the
  file (hoisted), or inject fakes through function/constructor
  parameters when the code supports it.
- Never make real network, filesystem, or database calls.
- Reset state between tests with `beforeEach(() => { jest.clearAllMocks() })`
  when mocks are used.

ASSERTIONS:
- Use Jest's `expect` API: `toBe`, `toEqual`, `toStrictEqual`,
  `toThrow`, `toHaveBeenCalledWith`, `resolves`/`rejects`.
- Prefer `toEqual` for objects/arrays, `toBe` for primitives.

CONSTRAINTS:
- Use ONLY dependencies that already exist in the project (the test
  framework itself plus what the source file imports). Do NOT add new
  packages.
- Tests must be deterministic: no timers without `jest.useFakeTimers()`,
  no reliance on wall-clock time or randomness.
- Do NOT modify or restate the source file.

OUTPUT:
- Respond with ONLY the complete test file content. No explanations,
  no markdown fences, no commentary before or after the code.
"""

SYSTEM_PROMPT_INCREMENTAL = """\
You are an expert JavaScript/TypeScript test engineer adding tests to
an EXISTING Jest/Vitest test file in a Node.js project.

You will be shown the existing test file (with outdated tests already
removed), the changed source functions, and what specifically changed.

== Requirements ==

- Return ONLY new test blocks: `describe(...)` / `test(...)` calls.
  NO imports, NO file wrapper, NO test-runner config. The blocks will
  be appended to the end of the existing file, which already has its
  imports.
- Use only what the existing file already imports or defines (helpers,
  fixtures, mocks). If you genuinely need another module, call
  `require('<module>')` INSIDE the test body — do not emit an import
  statement.
- Every test title MUST start with the exact source function name
  followed by a space (tooling depends on this prefix).
- Match the existing file's style: quote style, `test` vs `it`,
  assertion patterns, indentation.
- Cover the CHANGED behavior specifically — the diff is shown to you.
  Do not re-test unchanged behavior that surviving tests already cover.
- Tests must be deterministic and must not perform real I/O.

OUTPUT:
- Respond with ONLY the new test blocks. No explanations, no markdown
  fences, no commentary.
"""

SYSTEM_PROMPT_FIX = """\
You are an expert JavaScript/TypeScript test engineer fixing a failing
Jest/Vitest test file in a Node.js project.

You will be shown the source file under test, the current test file
content, and the test runner's output.

== Requirements ==

- Fix ONLY the tests — never suggest changing the source file. If a
  test's expectation contradicts the source's actual behavior, the
  test is wrong: align it with the source.
- Keep every passing test exactly as it is.
- Keep the file's module system (require vs import) exactly as it is.
- Preserve the test-title convention: titles start with the source
  function name.
- Do not add new package dependencies.

OUTPUT:
- Respond with ONLY the complete corrected test file content. No
  explanations, no markdown fences, no commentary.
"""


# ============================================================================
# User prompt templates
# ============================================================================

_USER_TEMPLATE_FRESH = """\
Generate a new test file for the changed functions below.

SOURCE FILE: {source_file}
TEST FILE TO CREATE: {test_file_path}
IMPORT THE MODULE UNDER TEST AS: {import_specifier}
  (relative specifier from the test file's directory; add named/default
  imports as the source's exports require)

== MODULE CONTEXT (signatures in the source file) ==

{class_context}

== CHANGED FUNCTIONS (full source) ==

{functions_code}

== WHAT CHANGED (diff hunks — focus tests here) ==

{diff_hunks}
"""

_USER_TEMPLATE_INCREMENTAL = """\
Add tests for the changed functions below to an existing test file.

SOURCE FILE: {source_file}
EXISTING TEST FILE: {test_file_path}

== MODULE CONTEXT (signatures in the source file) ==

{class_context}

== CHANGED FUNCTIONS (full source) ==

{functions_code}

== WHAT CHANGED (diff hunks — focus tests here) ==

{diff_hunks}

== TESTS THAT WERE REMOVED (they covered old behavior of these
functions — use them as a style reference, then write replacements
that match the NEW behavior) ==

{removed_tests_code}

== EXISTING TEST FILE (your new blocks will be appended to this —
use its imports and helpers; do not repeat its tests) ==

{trimmed_existing_content}
"""

_USER_TEMPLATE_FIX = """\
The following test file has failures. Fix the tests so they pass
against the source's ACTUAL behavior.

SOURCE FILE: {source_file}

== SOURCE CONTENT ==

{source_code}

== CURRENT TEST FILE ({test_file_path}) ==

{test_content}

== TEST RUNNER OUTPUT ==

{runner_output}
"""


# ============================================================================
# Prompt builders
# ============================================================================


def user_prompt_fresh(
    source_path: str,
    affected: list[AffectedFunction],
    test_file_path: str,
) -> str:
    return _USER_TEMPLATE_FRESH.format(
        source_file=source_path,
        test_file_path=test_file_path,
        import_specifier=derive_import_specifier(source_path, test_file_path),
        class_context=_format_class_context(affected),
        functions_code=_render_functions(affected),
        diff_hunks=_format_diff_hunks(affected),
    )


def user_prompt_incremental(
    source_path: str,
    existing: ExistingTest,
    affected: list[AffectedFunction],
    trimmed_existing_content: str,
    removed_tests_code: str,
) -> str:
    return _USER_TEMPLATE_INCREMENTAL.format(
        source_file=source_path,
        test_file_path=existing.test_file_path,
        class_context=_format_class_context(affected),
        functions_code=_render_functions(affected),
        diff_hunks=_format_diff_hunks(affected),
        removed_tests_code=(
            removed_tests_code
            or "(no previous tests covered these source functions)"
        ),
        trimmed_existing_content=trimmed_existing_content,
    )


def user_prompt_fix(generated: GeneratedTest, runner_output: str) -> str:
    source_code = "(source file content unavailable)"
    try:
        with open(generated.source_file_path, encoding="utf-8") as fh:
            source_code = fh.read()
    except OSError:
        pass

    return _USER_TEMPLATE_FIX.format(
        source_file=generated.source_file_path,
        test_file_path=generated.test_file_path,
        source_code=source_code,
        test_content=generated.content,
        runner_output=runner_output,
    )


def derive_import_specifier(source_path: str, test_file_path: str) -> str:
    """Relative import specifier from the test file to the source module.

    ``src/utils/format.ts`` tested from ``src/utils/format.test.ts``
    → ``./format``. Extension is dropped (Node/TS resolution adds it);
    separators are normalized to ``/`` (import paths are POSIX even on
    Windows).
    """
    source_no_ext = os.path.splitext(source_path)[0]
    rel = os.path.relpath(source_no_ext, os.path.dirname(test_file_path))
    rel = rel.replace(os.sep, "/")
    if not rel.startswith("."):
        rel = f"./{rel}"
    return rel


# ---------------------------------------------------------------------------
# Rendering helpers (same size-aware rules as the Kotlin prompts)
# ---------------------------------------------------------------------------


def _render_functions(affected: list[AffectedFunction]) -> str:
    return "\n\n".join(_render_one(fn) for fn in affected)


def _render_one(fn: AffectedFunction) -> str:
    """Compact rendering for big functions with small diffs — signature
    plus changed lines instead of the full body (see the Kotlin prompts
    module for the rationale).
    """
    source = fn.source_code
    diff = fn.diff_hunk.strip()

    if not diff or len(source) < 500 or len(diff) >= 0.30 * len(source):
        return source

    signature = source.splitlines()[0] if source else ""
    return (
        f"// {fn.name}: full body omitted because the change is small "
        f"({len(diff)} chars in a {len(source)}-char function).\n"
        f"{signature}\n"
        f"// ... (other lines unchanged — see WHAT CHANGED section) ...\n"
        f"{diff}"
    )


def _format_diff_hunks(affected: list[AffectedFunction]) -> str:
    if not affected:
        return "(no affected functions)"
    sections: list[str] = []
    for fn in affected:
        if fn.diff_hunk.strip():
            sections.append(
                f"--- In {fn.name} (lines {fn.line_start}-{fn.line_end}): ---\n"
                f"{fn.diff_hunk}"
            )
        else:
            sections.append(
                f"--- In {fn.name}: (diff hunk unavailable — assume the "
                f"entire function is the change) ---"
            )
    return "\n\n".join(sections)


def _format_class_context(affected: list[AffectedFunction]) -> str:
    if not affected:
        return "(no signatures available)"
    ctx = affected[0].class_context.strip()
    if not ctx:
        return (
            "(signatures unavailable — infer them from the function "
            "source below)"
        )
    return ctx
