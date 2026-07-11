"""Tests for the deterministic generated-test cache.

Determinism is a dev-trust requirement: running the tool three times on
unchanged code must produce the SAME tests, not three different ones.
The cache keys output on a hash of the shaping inputs; a hit returns
the stored content and skips the LLM.
"""

from __future__ import annotations

from test_automator.utils import gen_cache


def _use_tmp_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_AUTOMATOR_CACHE_DIR", str(tmp_path / "cache"))


def test_key_is_stable_and_order_independent():
    k1 = gen_cache.compute_key(
        source_path="src/a.js",
        function_sources=["fn foo", "fn bar"],
        mode="fresh",
    )
    k2 = gen_cache.compute_key(
        source_path="src/a.js",
        function_sources=["fn bar", "fn foo"],  # different order
        mode="fresh",
    )
    assert k1 == k2  # order-independent


def test_key_changes_when_source_changes():
    base = gen_cache.compute_key(
        source_path="src/a.js", function_sources=["v1"], mode="fresh"
    )
    changed = gen_cache.compute_key(
        source_path="src/a.js", function_sources=["v2"], mode="fresh"
    )
    assert base != changed


def test_key_changes_with_mode_and_existing_content():
    fresh = gen_cache.compute_key(
        source_path="a", function_sources=["x"], mode="fresh"
    )
    incr = gen_cache.compute_key(
        source_path="a", function_sources=["x"], mode="incremental"
    )
    incr2 = gen_cache.compute_key(
        source_path="a", function_sources=["x"], mode="incremental",
        existing_content="already here",
    )
    assert fresh != incr != incr2 and fresh != incr2


def test_put_then_get_roundtrips(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    key = gen_cache.compute_key(
        source_path="src/a.py", function_sources=["def f(): pass"],
        mode="fresh",
    )
    assert gen_cache.get("/repo", key) is None
    gen_cache.put("/repo", key, "GENERATED TESTS")
    assert gen_cache.get("/repo", key) == "GENERATED TESTS"


def test_cache_is_scoped_per_repo(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    key = gen_cache.compute_key(
        source_path="a", function_sources=["x"], mode="fresh"
    )
    gen_cache.put("/repo/one", key, "one")
    # Same key, different repo → miss (no cross-repo bleed)
    assert gen_cache.get("/repo/two", key) is None
    assert gen_cache.get("/repo/one", key) == "one"


def test_prompt_version_salt_invalidates(monkeypatch):
    k1 = gen_cache.compute_key(
        source_path="a", function_sources=["x"], mode="fresh"
    )
    monkeypatch.setattr(gen_cache, "PROMPT_VERSION", "999")
    k2 = gen_cache.compute_key(
        source_path="a", function_sources=["x"], mode="fresh"
    )
    assert k1 != k2
