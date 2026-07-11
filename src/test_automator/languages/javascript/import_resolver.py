"""Resolve a JS/TS file's relative imports to real signatures.

Same goal as the Java/Python resolvers: the model should be told the
exact shape of the modules it imports from the project, not left to
guess. For JS/TS we resolve RELATIVE imports (``./x``, ``../y``) — the
project-internal ones — to files in the repo and reuse the analyzer's
``extract_class_signatures`` to describe their exports. Bare specifiers
(``react``, ``lodash``, …) are node_modules and skipped.

Best-effort: any failure yields an empty block, never an exception.
"""

from __future__ import annotations

import os
import re

from test_automator.languages.javascript import analyzer

# import ... from '<path>' | export ... from '<path>' | import '<path>'
_FROM_RE = re.compile(
    r"""(?:import|export)\b[^;'"]*?from\s*['"]([^'"]+)['"]""",
    re.MULTILINE,
)
_BARE_IMPORT_RE = re.compile(r"""\bimport\s*['"]([^'"]+)['"]""")
_REQUIRE_RE = re.compile(r"""\brequire\(\s*['"]([^'"]+)['"]\s*\)""")

# Extensions to try when resolving a relative import with no extension,
# in priority order, plus index files inside a directory.
_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _import_paths(source_code: str) -> list[str]:
    paths: list[str] = []
    for rx in (_FROM_RE, _BARE_IMPORT_RE, _REQUIRE_RE):
        paths.extend(rx.findall(source_code))
    # Only project-internal (relative) imports.
    seen: list[str] = []
    for p in paths:
        if p.startswith(".") and p not in seen:
            seen.append(p)
    return seen


def _resolve(spec: str, source_file_path: str, repo_root: str) -> str | None:
    """Resolve a relative import specifier to an absolute file path."""
    src_dir = os.path.dirname(os.path.join(repo_root, source_file_path))
    target = os.path.normpath(os.path.join(src_dir, spec))

    # Exact file (spec already had an extension)?
    if os.path.isfile(target):
        return target
    # spec without extension → try each extension
    for ext in _EXTS:
        cand = target + ext
        if os.path.isfile(cand):
            return cand
    # directory import → index.<ext>
    for ext in _EXTS:
        cand = os.path.join(target, "index" + ext)
        if os.path.isfile(cand):
            return cand
    return None


def resolve_imports_block(
    source_code: str, source_file_path: str, repo_root: str
) -> str:
    if not repo_root or not source_file_path:
        return ""
    repo_root = os.path.abspath(repo_root)

    blocks: list[str] = []
    seen_files: set[str] = set()
    for spec in _import_paths(source_code):
        path = _resolve(spec, source_file_path, repo_root)
        if not path or path in seen_files:
            continue
        seen_files.add(path)
        try:
            with open(path, encoding="utf-8") as fh:
                target_src = fh.read()
        except OSError:
            continue
        sigs = analyzer.extract_class_signatures(target_src, file_path=path)
        if not sigs.strip():
            continue
        rel = os.path.relpath(path, repo_root)
        blocks.append(f"// from {rel}\n{sigs.strip()}")

    if not blocks:
        return ""

    return (
        "PROJECT MODULES you import (resolved from the codebase — use "
        "these EXACT exported names and signatures; do NOT guess or "
        "invent them):\n\n" + "\n\n".join(blocks)
    )
