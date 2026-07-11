"""Shared pytest fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_gen_cache(tmp_path, monkeypatch):
    """Point the generated-test cache at a fresh per-test temp dir.

    Without this, the cache would write into the developer's real
    ``~/.cache/test-automator`` during the suite — polluting their
    machine AND leaking hits between tests (a generation test would
    unexpectedly short-circuit on a prior test's cached output).
    """
    monkeypatch.setenv(
        "TEST_AUTOMATOR_CACHE_DIR", str(tmp_path / "_gen_cache")
    )
