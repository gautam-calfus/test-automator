"""Integration test: the analyzer drops functions that the diff touched
but that only changed by formatting (using each file's base content).

This is the uknowviews-react actions.js case: a Prettier reblock marked
~26 unchanged action-creators as 'changed'; only the genuinely-edited
ones should reach test generation.
"""

from __future__ import annotations

import os

from test_automator.config import LocalTestConfig
from test_automator.models import PRFile
from test_automator.steps.code_analyzer import CodeAnalyzer


def _write(root: str, rel: str, content: str) -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


# A Python module: foo() reformatted only; bar() logic actually changed.
_BASE = (
    "def foo(x):\n"
    "    return {'type':'A','value':x}\n"
    "\n"
    "def bar(x):\n"
    "    return x + 1\n"
)
_CURRENT = (
    "def foo(x):\n"
    "    return {'type': 'A', 'value': x}\n"   # formatting only
    "\n"
    "def bar(x):\n"
    "    return x - 1\n"                        # real change
)

# Patch marking BOTH functions' lines as changed (as a reblock would).
_PATCH = """\
--- a/mod.py
+++ b/mod.py
@@ -1,5 +1,5 @@
-def foo(x):
-    return {'type':'A','value':x}
+def foo(x):
+    return {'type': 'A', 'value': x}

-def bar(x):
-    return x + 1
+def bar(x):
+    return x - 1
"""


def test_formatting_only_function_is_dropped(tmp_path):
    root = str(tmp_path)
    _write(root, "mod.py", _CURRENT)

    cfg = LocalTestConfig(repo_path=root, base_branch="main")
    analyzer = CodeAnalyzer(cfg)

    pr_file = PRFile(
        filename="mod.py",
        status="modified",
        patch=_PATCH,
        base_content=_BASE,
    )
    affected = analyzer.analyze([pr_file])
    names = {fn.name for fn in affected}

    # foo changed only by formatting → dropped; bar really changed → kept
    assert names == {"bar"}, names


def test_no_base_content_keeps_everything(tmp_path):
    """Without base content (e.g. a new/untracked file) we can't tell
    formatting from behavior, so nothing is dropped."""
    root = str(tmp_path)
    _write(root, "mod.py", _CURRENT)

    cfg = LocalTestConfig(repo_path=root, base_branch="main")
    analyzer = CodeAnalyzer(cfg)

    pr_file = PRFile(
        filename="mod.py", status="added", patch=None, base_content=None
    )
    affected = analyzer.analyze([pr_file])
    # patch is None → all lines treated changed → both functions kept
    assert {fn.name for fn in affected} == {"foo", "bar"}
