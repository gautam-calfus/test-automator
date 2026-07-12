"""Find an existing test file in the repo to show the model as a
worked example of HOW THIS PROJECT WRITES TESTS.

This is the single biggest correctness lever for real codebases. A
React component test fails to compile not because the model can't write
RTL, but because it doesn't know THIS app's harness: which providers
wrap a render (Redux store, Router, theme, i18n), whether there's a
custom ``render`` helper, how the store is mocked. The same is true
elsewhere — pytest fixtures/conftest, a JUnit base test class, Kotlin
MockK setup conventions. One real passing test from the repo teaches
all of that far better than any generic instruction.

Language-agnostic: candidates are any file the handler recognizes as a
test file, sharing the source's extension family. We rank by directory
proximity to the source (a sibling test is most representative) and, for
component-style sources, prefer an example that actually renders
components (so the provider/harness setup is visible).
"""

from __future__ import annotations

import functools
import os

from test_automator._logging import get_logger

logger = get_logger(__name__)

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "build", "dist", "out",
    "__pycache__", ".gradle", ".idea", ".tox", "coverage", ".next",
    "site-packages",
}

# Signals that an example shows component/UI harness setup worth
# preferring when the source under test is itself a component.
_HARNESS_SIGNALS = (
    "@testing-library", "render(", "renderHook", "Provider",
    "createStore", "MemoryRouter", "BrowserRouter", "ThemeProvider",
    "mockStore", "wrapper",
)


@functools.lru_cache(maxsize=16)
def _all_test_files(repo_root: str, extensions: tuple[str, ...]) -> tuple[str, ...]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if name.endswith(extensions):
                found.append(os.path.join(dirpath, name))
    return tuple(found)


def _shared_prefix_len(a: str, b: str) -> int:
    pa, pb = a.split("/"), b.split("/")
    n = 0
    for x, y in zip(pa, pb, strict=False):
        if x != y:
            break
        n += 1
    return n


class ExampleFinder:
    """Locates a representative existing test file for a source file."""

    def __init__(self, config) -> None:
        self._config = config

    def find_example(
        self,
        handler,
        source_path: str,
        exclude_paths: set[str] | None = None,
        max_chars: int = 6000,
    ) -> tuple[str, str] | None:
        """Return ``(relative_path, content)`` of the best example test
        file, or None if the repo has no usable existing tests.

        ``exclude_paths`` (repo-relative) are skipped — e.g. the target
        file's own candidate test paths, so we never echo a file back
        to itself.
        """
        repo = self._config.repo_path
        exts = getattr(handler, "source_extensions", None) or (".py",)
        try:
            candidates = _all_test_files(os.path.abspath(repo), tuple(exts))
        except Exception:
            return None

        exclude = {self._norm(p) for p in (exclude_paths or set())}
        want_harness = self._looks_like_component(handler, source_path)

        best: tuple[int, str] | None = None  # (score, abs_path)
        for abs_path in candidates:
            rel = os.path.relpath(abs_path, os.path.abspath(repo))
            rel_norm = self._norm(rel)
            if rel_norm in exclude:
                continue
            try:
                if not handler.is_test_file(rel):
                    continue
            except Exception:
                continue
            score = _shared_prefix_len(rel_norm, self._norm(source_path)) * 10
            if want_harness:
                head = self._read(abs_path, 4000)
                if head and any(sig in head for sig in _HARNESS_SIGNALS):
                    score += 100
            # Prefer smaller, focused examples over sprawling ones.
            try:
                size = os.path.getsize(abs_path)
            except OSError:
                size = 10**9
            score -= size // 5000
            if best is None or score > best[0]:
                best = (score, abs_path)

        if best is None:
            return None

        content = self._read(best[1], max_chars)
        if not content or not content.strip():
            return None
        rel = os.path.relpath(best[1], os.path.abspath(repo))
        logger.info("using existing test as example | example=%s", rel)
        return rel, content

    @staticmethod
    def _looks_like_component(handler, source_path: str) -> bool:
        # React/UI sources benefit most from a harness example.
        return source_path.endswith((".jsx", ".tsx")) or getattr(
            handler, "name", ""
        ) == "javascript"

    @staticmethod
    def _norm(p: str) -> str:
        p = p.replace("\\", "/")
        return p[2:] if p.startswith("./") else p

    @staticmethod
    def _read(path: str, max_chars: int) -> str | None:
        try:
            with open(path, encoding="utf-8") as fh:
                data = fh.read(max_chars + 1)
        except OSError:
            return None
        if len(data) > max_chars:
            data = data[:max_chars] + "\n// … (example truncated) …\n"
        return data


def format_example_block(example: tuple[str, str] | None) -> str:
    """Prompt section presenting the example. Empty when none found."""
    if not example:
        return ""
    rel, content = example
    return (
        "HOW THIS PROJECT WRITES TESTS — an existing test file from this "
        "repo is shown below. MIRROR its setup: the same imports, test "
        "framework, provider/render helpers, mocking style, and file "
        "conventions. Do not invent a different harness.\n"
        f"--- {rel} ---\n{content}\n--- end example ---\n\n"
    )
