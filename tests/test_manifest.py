"""Unit tests for the embedded coverage manifest (utils.manifest)."""

from __future__ import annotations

from test_automator.utils import manifest


def test_fn_hash_stable_and_sensitive():
    a = manifest.fn_hash("def f(): return 1")
    assert a == manifest.fn_hash("def f(): return 1")   # deterministic
    assert a != manifest.fn_hash("def f(): return 2")   # source-sensitive
    assert len(a) == 16


def test_render_is_sorted_and_deterministic():
    block1 = manifest.render({"b": "22", "a": "11"}, "#")
    block2 = manifest.render({"a": "11", "b": "22"}, "#")
    assert block1 == block2                     # order-independent
    assert block1.index(" a ") < block1.index(" b ")


def test_roundtrip_parse_python():
    content = manifest.inject(
        "import x\n\n\ndef test_a():\n    pass\n",
        {"pkg.A.foo": "abc123abc123abc1"},
        "#",
    )
    assert content.startswith("# test-automator:begin")
    parsed = manifest.parse(content)
    assert parsed == {"pkg.A.foo": "abc123abc123abc1"}
    # dotted/qualified names survive
    assert "pkg.A.foo" in parsed


def test_roundtrip_parse_java_slashes():
    content = manifest.inject(
        "package com.acme;\n\nclass T {}\n",
        {"com.acme.Svc.create": "0011223344556677"},
        "//",
    )
    assert content.startswith("// test-automator:begin")
    assert manifest.parse(content) == {"com.acme.Svc.create": "0011223344556677"}


def test_inject_is_idempotent():
    body = "def test_a():\n    pass\n"
    once = manifest.inject(body, {"foo": "aaaaaaaaaaaaaaaa"}, "#")
    twice = manifest.inject(once, {"foo": "aaaaaaaaaaaaaaaa"}, "#")
    assert once == twice                         # no stacking
    # exactly one block
    assert once.count("test-automator:begin") == 1
    assert twice.count("test-automator:begin") == 1


def test_inject_merges_and_updates_entries():
    body = "x\n"
    v1 = manifest.inject(body, {"foo": "1111111111111111"}, "#")
    # add bar, update foo — old block replaced, both entries present
    v2 = manifest.inject(v1, {"foo": "2222222222222222",
                              "bar": "3333333333333333"}, "#")
    parsed = manifest.parse(v2)
    assert parsed == {"foo": "2222222222222222", "bar": "3333333333333333"}
    assert v2.count("test-automator:begin") == 1


def test_inject_preserves_untouched_entries():
    body = "x\n"
    v1 = manifest.inject(body, {"foo": "1111111111111111",
                               "bar": "2222222222222222"}, "#")
    # regenerate only foo this run
    v2 = manifest.inject(v1, {"foo": "9999999999999999"}, "#")
    parsed = manifest.parse(v2)
    assert parsed["foo"] == "9999999999999999"
    assert parsed["bar"] == "2222222222222222"   # preserved


def test_strip_removes_block_only():
    body = "import x\n\n\ndef test_a():\n    pass\n"
    content = manifest.inject(body, {"foo": "aaaaaaaaaaaaaaaa"}, "#")
    stripped = manifest.strip(content)
    assert "test-automator" not in stripped
    assert "def test_a" in stripped
    assert "import x" in stripped


def test_parse_empty_when_no_block():
    assert manifest.parse("just some code\n") == {}
    assert manifest.parse("") == {}


def test_inject_noop_for_empty_hashes():
    body = "code\n"
    assert manifest.inject(body, {}, "#") == body
