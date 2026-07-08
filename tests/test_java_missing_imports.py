"""Tests for v0.2: auto-add missing imports for repo classes the
generated test references in its body but never imports.

Real Acme run: Claude used ``CredentialsService`` in a CMService test
body but wrote no import for it. verify_test_imports only rewrites
existing import LINES, so nothing was fixed and the file failed to
compile. add_missing_imports scans the body and inserts the import
when the repo index has a unique match for the simple name.
"""

from __future__ import annotations

import os
import textwrap

from test_automator.languages.java import repo_index
from test_automator.languages.java.import_resolver import add_missing_imports


def _repo(root):
    os.makedirs(f"{root}/src/main/java/com/acme/vault/config", exist_ok=True)
    os.makedirs(f"{root}/src/main/java/com/acme/service", exist_ok=True)
    open(
        f"{root}/src/main/java/com/acme/vault/config/CredentialsService.java",
        "w",
    ).write("package com.acme.vault.config;\npublic class CredentialsService {}\n")
    open(
        f"{root}/src/main/java/com/acme/service/CMService.java", "w"
    ).write("package com.acme.service;\npublic class CMService {}\n")
    # Clear the module-level index cache so each tmp repo is fresh
    repo_index._INDEX_CACHE.clear()


def test_adds_import_for_unimported_body_reference(tmp_path):
    _repo(str(tmp_path))
    code = textwrap.dedent("""\
        package com.acme.service;

        import org.junit.jupiter.api.Test;

        class CMServiceTest {
            @Test
            void usesCredentials() {
                CredentialsService creds = mock(CredentialsService.class);
            }
        }
        """)

    fixed, additions = add_missing_imports(code, str(tmp_path))

    assert "com.acme.vault.config.CredentialsService" in additions
    assert "import com.acme.vault.config.CredentialsService;" in fixed
    # inserted after the existing import, before the class
    assert fixed.index("import com.acme.vault.config") < fixed.index("class CMServiceTest")


def test_does_not_import_same_package_class(tmp_path):
    _repo(str(tmp_path))
    code = textwrap.dedent("""\
        package com.acme.service;

        class CMServiceTest {
            void x() { CMService s = new CMService(); }
        }
        """)
    fixed, additions = add_missing_imports(code, str(tmp_path))
    assert additions == []
    assert "import com.acme.service.CMService;" not in fixed


def test_does_not_import_unknown_class(tmp_path):
    _repo(str(tmp_path))
    code = textwrap.dedent("""\
        package com.acme.service;

        class CMServiceTest {
            void x() { SomeLibraryThing t = null; }
        }
        """)
    _, additions = add_missing_imports(code, str(tmp_path))
    assert additions == []


def test_does_not_duplicate_existing_import(tmp_path):
    _repo(str(tmp_path))
    code = textwrap.dedent("""\
        package com.acme.service;

        import com.acme.vault.config.CredentialsService;

        class CMServiceTest {
            void x() { CredentialsService c = null; }
        }
        """)
    fixed, additions = add_missing_imports(code, str(tmp_path))
    assert additions == []
    assert fixed.count("import com.acme.vault.config.CredentialsService;") == 1
