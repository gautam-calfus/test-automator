"""Resolve a Kotlin file's project-internal imports to real signatures.

Kotlin imports are fully-qualified (``import com.acme.User``), so — like
Java — we build a repo index mapping FQN -> file by scanning each
``.kt`` file's ``package`` line and top-level declarations. For the
changed file, each ``import`` that resolves to an indexed FQN is read
and described via the analyzer's ``extract_class_signatures``, so the
model uses exact constructor/method signatures instead of guessing.

Best-effort: any failure yields an empty block, never an exception.
Third-party / stdlib imports (not in the repo index) are skipped, as
are wildcard imports.
"""

from __future__ import annotations

import functools
import os
import re

from test_automator.languages.kotlin import analyzer

_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)", re.MULTILINE)
_IMPORT_RE = re.compile(r"^\s*import\s+([\w.]+)(\.\*)?\s*$", re.MULTILINE)
# Top-level declarations whose simple name combines with the package
# to form the FQN other files import.
_DECL_RE = re.compile(
    r"^\s*(?:public\s+|internal\s+|abstract\s+|open\s+|sealed\s+|data\s+|"
    r"enum\s+|final\s+)*"
    r"(?:class|object|interface)\s+([A-Z]\w*)",
    re.MULTILINE,
)


@functools.lru_cache(maxsize=32)
def _fqn_index(repo_root: str) -> dict[str, str]:
    """Map ``package.ClassName`` -> absolute file path for every
    top-level class/object/interface under ``repo_root``."""
    index: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [
            d for d in dirnames
            if d not in {
                ".git", ".gradle", "build", "out", "node_modules", ".idea",
            }
        ]
        for name in filenames:
            if not name.endswith(".kt"):
                continue
            abs_path = os.path.join(dirpath, name)
            try:
                with open(abs_path, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                continue
            pkg_m = _PACKAGE_RE.search(text)
            pkg = pkg_m.group(1) if pkg_m else ""
            for decl in _DECL_RE.findall(text):
                fqn = f"{pkg}.{decl}" if pkg else decl
                index.setdefault(fqn, abs_path)
    return index


def resolve_imports_block(
    source_code: str, source_file_path: str, repo_root: str
) -> str:
    if not repo_root or not source_file_path:
        return ""
    repo_root = os.path.abspath(repo_root)
    try:
        index = _fqn_index(repo_root)
    except Exception:
        return ""
    if not index:
        return ""

    blocks: list[str] = []
    seen_files: set[str] = set()
    for fqn, wildcard in _IMPORT_RE.findall(source_code):
        if wildcard:
            continue  # can't resolve `import pkg.*` to one file
        path = index.get(fqn)
        if not path or path in seen_files:
            continue
        seen_files.add(path)
        try:
            with open(path, encoding="utf-8") as fh:
                target_src = fh.read()
        except OSError:
            continue
        sigs = analyzer.extract_class_signatures(target_src)
        if not sigs.strip():
            continue
        rel = os.path.relpath(path, repo_root)
        blocks.append(f"// from {rel} ({fqn})\n{sigs.strip()}")

    if not blocks:
        return ""

    return (
        "PROJECT CLASSES you import (resolved from the codebase — use "
        "these EXACT constructor and method signatures; do NOT guess or "
        "invent them):\n\n" + "\n\n".join(blocks)
    )
