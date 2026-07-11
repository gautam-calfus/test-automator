"""Resolve a Python file's project-internal imports to real signatures.

Why: the model should never GUESS the shape of a class or function it
imports — it should be told the exact signature from the codebase. This
mirrors the Java repo-index approach for Python, using the stdlib
``ast`` module (no third-party parser needed).

Given the changed source file, we:
1. parse its ``import`` / ``from ... import ...`` statements,
2. resolve each project-internal module to a file in the repo,
3. extract the signatures of the imported names (class __init__ +
   public methods, or function signatures) from that file,
4. format a compact block for the prompt.

Everything is best-effort: any parse/resolution failure yields an empty
block rather than breaking the pipeline. Third-party / stdlib imports
(not resolvable to a repo file) are skipped.
"""

from __future__ import annotations

import ast
import functools
import os


@functools.lru_cache(maxsize=64)
def _module_index(repo_root: str) -> dict[str, str]:
    """Map dotted module name -> absolute file path for every ``.py``
    file under ``repo_root``.

    A file ``<root>/a/b/c.py`` is indexed as ``a.b.c`` AND, when it
    lives under a common source root (``src/``, ``app/``, ``lib/``),
    also under the root-relative dotted name (``src.a.b`` and ``a.b``),
    so imports resolve whether or not the project uses a ``src`` layout.
    A package ``__init__.py`` is indexed as its directory's dotted name.
    """
    index: dict[str, str] = {}
    src_roots = ("src", "app", "lib", "")
    for dirpath, dirnames, filenames in os.walk(repo_root):
        # Skip virtualenvs, caches, VCS, build artifacts
        dirnames[:] = [
            d for d in dirnames
            if d not in {
                ".git", ".venv", "venv", "__pycache__", "node_modules",
                "build", "dist", ".tox", ".mypy_cache", ".pytest_cache",
                "site-packages",
            }
        ]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            abs_path = os.path.join(dirpath, name)
            rel = os.path.relpath(abs_path, repo_root).replace(os.sep, "/")
            no_ext = rel[: -len(".py")]
            parts = no_ext.split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if not parts:
                continue
            # Full path-based dotted name
            index.setdefault(".".join(parts), abs_path)
            # Also without a leading source-root segment
            if parts[0] in src_roots and len(parts) > 1:
                index.setdefault(".".join(parts[1:]), abs_path)
    return index


def _imported_targets(
    tree: ast.Module,
    source_file_path: str,
    repo_root: str,
    index: dict[str, str],
) -> dict[str, set[str] | None]:
    """Return ``{abs_file_path: {names} | None}`` for project-internal
    imports. ``None`` means the whole module was imported (``import x``
    / ``from x import *``) so we surface all its top-level symbols.
    """
    targets: dict[str, set[str] | None] = {}

    def add(path: str, names: set[str] | None) -> None:
        if path is None:
            return
        if path not in targets:
            targets[path] = set() if names is not None else None
        if names is None:
            targets[path] = None
        elif targets[path] is not None:
            targets[path] |= names

    # For relative imports, resolve against the source file's package.
    # source_file_path may be repo-relative or absolute; normalize to a
    # path relative to repo_root either way.
    abs_src = (
        source_file_path
        if os.path.isabs(source_file_path)
        else os.path.join(repo_root, source_file_path)
    )
    src_rel = os.path.relpath(abs_src, repo_root).replace(os.sep, "/")
    src_pkg_parts = src_rel[: -len(".py")].split("/")[:-1]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                path = index.get(alias.name)
                if path:
                    add(path, None)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # Relative import: climb `level-1` packages from the
                # source file's package, then append the module.
                base = src_pkg_parts[: len(src_pkg_parts) - (node.level - 1)]
                mod_parts = base + (node.module.split(".") if node.module else [])
                mod = ".".join(mod_parts)
            else:
                mod = node.module or ""
            mod_path = index.get(mod)
            if mod_path:
                add(mod_path, {a.name for a in node.names if a.name != "*"}
                    or None)
            else:
                # ``from pkg import Sub`` where Sub is itself a module.
                for alias in node.names:
                    sub = index.get(f"{mod}.{alias.name}") if mod else None
                    if sub:
                        add(sub, None)
    return targets


def _signatures_for(path: str, names: set[str] | None) -> list[str]:
    """Extract class/function signatures from ``path``. If ``names`` is
    given, only those top-level symbols; otherwise all public ones."""
    try:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError):
        return []

    out: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            nm = node.name
            if names is not None and nm not in names:
                continue
            if names is None and nm.startswith("_"):
                continue
            if isinstance(node, ast.ClassDef):
                out.append(_class_sig(node))
            else:
                out.append(_func_sig(node) + "  # module-level function")
    return out


def _func_sig(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = _format_args(node.args)
    ret = ""
    if node.returns is not None:
        ret = f" -> {_annotation(node.returns)}"
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({args}){ret}"


def _class_sig(node: ast.ClassDef) -> str:
    bases = ", ".join(_annotation(b) for b in node.bases)
    header = f"class {node.name}({bases}):" if bases else f"class {node.name}:"
    lines = [header]
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if item.name.startswith("_") and item.name != "__init__":
                continue
            lines.append("    " + _func_sig(item))
    if len(lines) == 1:
        lines.append("    ...")
    return "\n".join(lines)


def _format_args(args: ast.arguments) -> str:
    parts: list[str] = []
    posonly = getattr(args, "posonlyargs", [])
    for a in list(posonly) + list(args.args):
        parts.append(_one_arg(a))
    if args.vararg:
        parts.append("*" + _one_arg(args.vararg))
    for a in args.kwonlyargs:
        parts.append(_one_arg(a))
    if args.kwarg:
        parts.append("**" + _one_arg(args.kwarg))
    return ", ".join(parts)


def _one_arg(a: ast.arg) -> str:
    if a.annotation is not None:
        return f"{a.arg}: {_annotation(a.annotation)}"
    return a.arg


def _annotation(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "?"


def resolve_imports_block(
    source_code: str, source_file_path: str, repo_root: str
) -> str:
    """Formatted prompt block of signatures for the file's
    project-internal imports, or "" when there are none / on any error.
    """
    if not repo_root or not source_file_path:
        return ""
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return ""
    try:
        index = _module_index(os.path.abspath(repo_root))
    except Exception:
        return ""

    targets = _imported_targets(
        tree, source_file_path, os.path.abspath(repo_root), index
    )
    if not targets:
        return ""

    blocks: list[str] = []
    for path, names in sorted(targets.items()):
        sigs = _signatures_for(path, names)
        if not sigs:
            continue
        rel = os.path.relpath(path, os.path.abspath(repo_root))
        blocks.append(f"# from {rel}\n" + "\n".join(sigs))

    if not blocks:
        return ""

    return (
        "PROJECT SYMBOLS you import (resolved from the codebase — use "
        "these EXACT names and signatures; do NOT guess or invent "
        "them):\n\n" + "\n\n".join(blocks)
    )
