"""Repo-wide index of every .java file in the target repository.

Motivation (Gautam, Acme, July 2026): ``import_resolver`` used to map
FQN → file path by probing a hard-coded list of conventional source
roots (``src/main/java`` etc.) relative to the repo root. That fails
for anything else — multi-module Maven/Gradle repos
(``common/src/main/java/...``, ``service/src/main/java/...``), jOOQ
output under a module's ``build/``, or nonstandard layouts. When the
lookup missed, the resolver silently produced no signature and Claude
went back to inventing packages for the DAOs on ``Daos.java``.

Instead of guessing paths, walk the repo ONCE, read each file's
``package`` declaration, and build:

- ``fqn_to_path``:      ``com.acme.common.Daos`` → absolute path
- ``simple_to_fqns``:   ``Daos`` → [``com.acme.common.Daos``, ...]
- ``package_to_fqns``:  ``com.acme.common`` → [FQNs in that package]

This is the ground truth for "what are the correct imports" — every
consumer (import resolution, Lombok getter FQN hints, post-generation
import verification) reads from here.

The index is cached per repo root for the lifetime of the process, so
the walk happens once per run, not once per changed file.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# The package declaration sits at the top of the file (possibly after a
# license header). Reading the whole file for thousands of sources is
# wasteful — the first 8KB is more than enough.
_PACKAGE_SCAN_BYTES = 8_192

_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)

# Directory names that never contain project sources. NOTE: ``build``
# and ``target`` are NOT skipped wholesale — generated sources (jOOQ,
# annotation processors) live there and are exactly the DAOs we need.
_SKIP_DIR_NAMES = {
    ".git",
    ".gradle",
    ".idea",
    ".mvn",
    ".settings",
    "node_modules",
    "__pycache__",
}

# Files that declare no importable type.
_SKIP_FILE_NAMES = {"package-info.java", "module-info.java"}


@dataclass
class JavaRepoIndex:
    """Lookup tables over every .java file found under the repo root."""

    repo_root: str
    fqn_to_path: dict[str, str] = field(default_factory=dict)
    simple_to_fqns: dict[str, list[str]] = field(default_factory=dict)
    package_to_fqns: dict[str, list[str]] = field(default_factory=dict)

    def path_for_fqn(self, fqn: str) -> str | None:
        """Exact FQN → file path, or None if not in the repo."""
        return self.fqn_to_path.get(fqn)

    def fqns_for_simple_name(self, simple_name: str) -> list[str]:
        """All FQNs in the repo whose class name is ``simple_name``."""
        return self.simple_to_fqns.get(simple_name, [])

    def fqns_in_package(self, package: str) -> list[str]:
        """All FQNs declared directly in ``package`` (non-recursive)."""
        return self.package_to_fqns.get(package, [])

    def contains_fqn_or_outer(self, fqn: str) -> bool:
        """True if ``fqn`` is a known class OR a member/inner class of
        one (``com.x.Daos.Inner`` matches when ``com.x.Daos`` is known).
        Used to validate imports without rejecting inner-class or
        static-member imports.
        """
        parts = fqn.split(".")
        for end in range(len(parts), 1, -1):
            if ".".join(parts[:end]) in self.fqn_to_path:
                return True
        return False


def build_repo_index(repo_root: str) -> JavaRepoIndex:
    """Walk ``repo_root`` and index every .java file by its declared
    package. Files whose package can't be read are skipped.

    When the same FQN appears at multiple paths (e.g. a copy under
    ``build/`` and one under ``src/main/java``), the path containing
    a ``src/main`` segment wins.
    """
    index = JavaRepoIndex(repo_root=os.path.abspath(repo_root))

    for dirpath, dirnames, filenames in os.walk(index.repo_root):
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIR_NAMES and not d.startswith(".")
        ]
        for filename in filenames:
            if not filename.endswith(".java"):
                continue
            if filename in _SKIP_FILE_NAMES:
                continue
            file_path = os.path.join(dirpath, filename)
            package = _read_package(file_path)
            if package is None:
                continue
            simple_name = filename[: -len(".java")]
            fqn = f"{package}.{simple_name}"

            existing = index.fqn_to_path.get(fqn)
            if existing is not None:
                if not _prefer_over(file_path, existing):
                    continue
                index.fqn_to_path[fqn] = file_path
                continue

            index.fqn_to_path[fqn] = file_path
            index.simple_to_fqns.setdefault(simple_name, []).append(fqn)
            index.package_to_fqns.setdefault(package, []).append(fqn)

    return index


def _read_package(file_path: str) -> str | None:
    """Read the ``package`` declaration from the top of a .java file."""
    try:
        with open(file_path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(_PACKAGE_SCAN_BYTES)
    except OSError:
        return None
    match = _PACKAGE_RE.search(head)
    return match.group(1) if match else None


def _prefer_over(new_path: str, existing_path: str) -> bool:
    """Duplicate-FQN tie-break: prefer real main sources over copies in
    build output or tests.
    """
    def _rank(p: str) -> int:
        norm = p.replace("\\", "/")
        if "/src/main/" in norm:
            return 0
        if "/src/test/" in norm:
            return 2
        return 1

    return _rank(new_path) < _rank(existing_path)


# ---------------------------------------------------------------------------
# Process-lifetime cache — one walk per repo per run
# ---------------------------------------------------------------------------

_INDEX_CACHE: dict[str, JavaRepoIndex] = {}


def get_repo_index(repo_root: str) -> JavaRepoIndex:
    """Return the (cached) index for ``repo_root``, building on first use."""
    key = os.path.abspath(repo_root)
    cached = _INDEX_CACHE.get(key)
    if cached is None:
        cached = build_repo_index(key)
        _INDEX_CACHE[key] = cached
    return cached


def clear_repo_index_cache() -> None:
    """Drop all cached indexes (tests, or after files change on disk)."""
    _INDEX_CACHE.clear()
