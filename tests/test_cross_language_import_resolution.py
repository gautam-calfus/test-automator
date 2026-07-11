"""Cross-language import resolution: the model is told the REAL
signatures of project symbols a changed file imports, resolved from the
codebase, instead of guessing them.

Java already had this (repo index). These tests cover the parity
implementations for Python, JavaScript/TypeScript, and Kotlin.
"""

from __future__ import annotations

import os

from test_automator.languages.javascript import (
    import_resolver as js_resolver,
)
from test_automator.languages.kotlin import (
    import_resolver as kt_resolver,
)
from test_automator.languages.python import (
    import_resolver as py_resolver,
)


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


# --------------------------------------------------------------- Python
def test_python_resolves_imported_class_signature(tmp_path):
    root = str(tmp_path)
    _write(f"{root}/src/models/user.py",
           "class User:\n"
           "    def __init__(self, uid: int, email: str):\n"
           "        self.uid = uid\n"
           "    def deactivate(self, reason: str) -> bool:\n"
           "        return True\n"
           "    def _hidden(self):\n"
           "        pass\n")
    src = "from src.models.user import User\n\n" \
          "def go(u: User):\n    return u.deactivate('x')\n"
    _write(f"{root}/src/services/acct.py", src)

    block = py_resolver.resolve_imports_block(
        src, "src/services/acct.py", root
    )
    assert "class User" in block
    assert "def __init__(self, uid: int, email: str)" in block
    assert "def deactivate(self, reason: str) -> bool" in block
    assert "_hidden" not in block  # private methods excluded


def test_python_skips_stdlib_and_thirdparty(tmp_path):
    root = str(tmp_path)
    src = "import os\nimport requests\n\ndef f():\n    return os.getcwd()\n"
    _write(f"{root}/src/x.py", src)
    assert py_resolver.resolve_imports_block(src, "src/x.py", root) == ""


def test_python_resolves_relative_import(tmp_path):
    root = str(tmp_path)
    _write(f"{root}/pkg/__init__.py", "")
    _write(f"{root}/pkg/helpers.py",
           "def compute(a: int, b: int) -> int:\n    return a + b\n")
    src = "from .helpers import compute\n\ndef g():\n    return compute(1, 2)\n"
    _write(f"{root}/pkg/main.py", src)

    block = py_resolver.resolve_imports_block(src, "pkg/main.py", root)
    assert "def compute(a: int, b: int) -> int" in block


# ----------------------------------------------------------- JavaScript
def test_js_resolves_relative_import(tmp_path):
    root = str(tmp_path)
    _write(f"{root}/src/utils/format.js",
           "export function currency(amount, code) { return code + amount; }\n"
           "export const clamp = (n, lo, hi) => Math.max(lo, n);\n")
    src = "import { currency, clamp } from '../utils/format';\n" \
          "export function Price({ amount }) { return currency(amount, '$'); }\n"
    _write(f"{root}/src/components/Price.jsx", src)

    block = js_resolver.resolve_imports_block(
        src, "src/components/Price.jsx", root
    )
    assert "currency" in block and "clamp" in block
    assert "format.js" in block


def test_js_skips_node_modules_imports(tmp_path):
    root = str(tmp_path)
    src = "import React from 'react';\nimport _ from 'lodash';\n" \
          "export const x = () => React.createElement('div');\n"
    _write(f"{root}/src/a.js", src)
    assert js_resolver.resolve_imports_block(src, "src/a.js", root) == ""


def test_js_resolves_directory_index_import(tmp_path):
    root = str(tmp_path)
    _write(f"{root}/src/api/index.ts",
           "export function fetchUser(id: string) { return id; }\n")
    src = "import { fetchUser } from './api';\n" \
          "export function load(id: string) { return fetchUser(id); }\n"
    _write(f"{root}/src/load.ts", src)

    block = js_resolver.resolve_imports_block(src, "src/load.ts", root)
    assert "fetchUser" in block


# --------------------------------------------------------------- Kotlin
def test_kotlin_resolves_fqn_import(tmp_path):
    root = str(tmp_path)
    _write(f"{root}/src/main/kotlin/com/acme/User.kt",
           "package com.acme\n"
           "data class User(val id: Int, val email: String)\n")
    src = "package com.acme\nimport com.acme.User\n" \
          "class Svc { fun go(u: User) = u.id }\n"
    _write(f"{root}/src/main/kotlin/com/acme/Svc.kt", src)

    block = kt_resolver.resolve_imports_block(
        src, "src/main/kotlin/com/acme/Svc.kt", root
    )
    assert "User" in block
    assert "com.acme.User" in block


def test_kotlin_skips_stdlib_and_wildcards(tmp_path):
    root = str(tmp_path)
    _write(f"{root}/src/main/kotlin/com/acme/Svc.kt", "package com.acme\n")
    src = "package com.acme\n" \
          "import kotlin.collections.List\nimport com.other.*\n" \
          "class Svc\n"
    _write(f"{root}/src/main/kotlin/com/acme/Svc.kt", src)
    assert kt_resolver.resolve_imports_block(
        src, "src/main/kotlin/com/acme/Svc.kt", root
    ) == ""
