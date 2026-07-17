"""Idempotency manifest embedded inside each generated test file.

Records, per source function the tool has generated tests for, a hash of
that function's source at generation time — as a comment block in the
test file itself:

    // test-automator:begin — generated coverage manifest, do not edit
    // com.acme.AdminService.createNewUsers 5f2c9a1b3d4e6f70
    // test-automator:end

Why in the file (not a side cache): the signal has to travel with the
committed test so a re-run on ANY machine — a teammate's laptop, CI —
can tell a function's tests are already up to date and skip regeneration.
A per-machine cache can't do that.

Why hashes (not test names): the old idempotency check asked "does a test
NAME match this method name?". Real LLM output names tests for behavior
(``shouldInsertUserRoutingLevelWhen…``), never for the method, so that
check silently failed and every re-run churned the file. A source hash is
independent of how tests are named: unchanged source ⇒ same hash ⇒ skip;
changed source ⇒ different hash ⇒ regenerate. It self-invalidates.

The block is deterministically delimited so a regeneration REPLACES it
instead of stacking copies, and entries are emitted in sorted order so
re-stamping identical inputs produces byte-identical output.
"""

from __future__ import annotations

import hashlib
import re

BEGIN = "test-automator:begin"
END = "test-automator:end"
_HEADER = f"{BEGIN} — generated coverage manifest, do not edit"

# The whole block, on either comment token, including its trailing
# newline(s). Non-greedy body so only ONE block is consumed.
_BLOCK_RE = re.compile(
    r"^[ \t]*(?:#|//)[ \t]*" + re.escape(BEGIN) + r".*?"
    r"^[ \t]*(?:#|//)[ \t]*" + re.escape(END) + r"[ \t]*\n?",
    re.DOTALL | re.MULTILINE,
)
# A single "name hash" entry line within the block.
_ENTRY_RE = re.compile(
    r"^[ \t]*(?:#|//)[ \t]*([A-Za-z_$][\w$.]*)[ \t]+([0-9a-f]{6,64})[ \t]*$",
    re.MULTILINE,
)


def fn_hash(source_code: str) -> str:
    """Stable short hash of a function's source. Truncated sha256 — 16
    hex chars is ample collision resistance for one file's functions and
    keeps the manifest readable."""
    return hashlib.sha256((source_code or "").encode("utf-8")).hexdigest()[:16]


def parse(content: str) -> dict[str, str]:
    """Return ``{function_name: hash}`` from the manifest block, or ``{}``
    when there is no block (legacy or hand-written test files)."""
    m = _BLOCK_RE.search(content or "")
    if not m:
        return {}
    return {name: h for name, h in _ENTRY_RE.findall(m.group(0))}


def strip(content: str) -> str:
    """Remove the first manifest block, if any."""
    return _BLOCK_RE.sub("", content or "", count=1)


def render(fn_to_hash: dict[str, str], comment: str) -> str:
    """Render a manifest block using ``comment`` as the line-comment
    token (``#`` or ``//``). Entries are sorted for deterministic output.
    """
    lines = [f"{comment} {_HEADER}"]
    for name in sorted(fn_to_hash):
        lines.append(f"{comment} {name} {fn_to_hash[name]}")
    lines.append(f"{comment} {END}")
    return "\n".join(lines)


def inject(content: str, fn_to_hash: dict[str, str], comment: str) -> str:
    """Embed/refresh the manifest at the TOP of the file.

    Merges ``fn_to_hash`` over any existing manifest so functions covered
    by earlier runs but not touched this run keep their entries. Idempotent:
    same inputs ⇒ same output.
    """
    if not fn_to_hash:
        return content
    merged = {**parse(content), **fn_to_hash}
    body = strip(content).lstrip("\n")
    block = render(merged, comment)
    return f"{block}\n\n{body}" if body.strip() else f"{block}\n"
