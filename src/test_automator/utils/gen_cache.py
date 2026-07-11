"""Content-addressed cache for generated tests.

Purpose: determinism + token savings. LLMs are non-deterministic, so
running the tool three times on the same code would otherwise produce
three different test files — devs won't trust or adopt that. This cache
keys generated output on a hash of the exact inputs that shaped it
(source path, the changed functions' source, the mode, any existing
test content, and a prompt-version salt). On a re-run where none of
that changed, the cached test file is returned verbatim and NO LLM call
is made: identical output, near-zero tokens.

When any input changes — the code was edited, the prompt template was
revised (bump ``PROMPT_VERSION``) — the hash changes and the entry is
regenerated, so stale output is never served.

Cache lives outside the repo (under the OS cache dir, keyed by repo
path) so it never shows up in ``git status`` or the working-tree diff.
"""

from __future__ import annotations

import hashlib
import os

# Bump when prompt templates or extraction logic change materially, so
# cached output from an older prompt is naturally invalidated.
PROMPT_VERSION = "1"


def _cache_root(repo_path: str) -> str:
    base = os.environ.get("TEST_AUTOMATOR_CACHE_DIR") or os.path.join(
        os.path.expanduser("~"), ".cache", "test-automator"
    )
    repo_key = hashlib.sha256(
        os.path.abspath(repo_path).encode("utf-8")
    ).hexdigest()[:16]
    return os.path.join(base, repo_key)


def compute_key(
    *,
    source_path: str,
    function_sources: list[str],
    mode: str,
    existing_content: str = "",
) -> str:
    """Stable hash of everything that determines the generated output.

    ``function_sources`` is sorted so the key is independent of the
    order functions happen to be discovered in.
    """
    h = hashlib.sha256()
    h.update(PROMPT_VERSION.encode("utf-8"))
    h.update(b"\x00")
    h.update(source_path.encode("utf-8"))
    h.update(b"\x00")
    h.update(mode.encode("utf-8"))
    h.update(b"\x00")
    for src in sorted(function_sources):
        h.update(src.encode("utf-8"))
        h.update(b"\x00")
    h.update(existing_content.encode("utf-8"))
    return h.hexdigest()


def get(repo_path: str, key: str) -> str | None:
    """Return cached generated content for ``key``, or None on miss."""
    path = os.path.join(_cache_root(repo_path), key + ".txt")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def put(repo_path: str, key: str, content: str) -> None:
    """Store generated ``content`` under ``key``. Best-effort: a cache
    write failure must never break the pipeline."""
    root = _cache_root(repo_path)
    try:
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, key + ".txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    except OSError:
        pass
