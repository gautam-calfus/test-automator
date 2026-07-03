"""Tests for v0.3.0a7: transitive import resolution and inner enum extraction.

Grounded in the exact bugs from Gautam's UserDeactivationServiceTest run:

Bug 1: Claude picked jOOQ-generated ``UserDao`` from ``com.acme.domains.core.tables.daos``
       when Daos.getUserDao() actually returns ``com.acme.service.daos.UserDaoImpl``.
       Cause: Claude never saw UserDaoImpl's declaration — the source only imported
       Daos, not UserDaoImpl. Transitive resolution fixes this by following Daos's
       imports.

Bug 2: Claude used ``Object`` for ``DeactivationCommand.Reason`` and ``SourceIdp``
       because inner enums weren't extracted from the class signature. Now they are.
"""

from __future__ import annotations

import os
import textwrap

import pytest

from pr_test_automator_local.languages.java.import_resolver import (
    resolve_imports,
)


def _write(root: str, rel_path: str, content: str):
    full = os.path.join(root, rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(textwrap.dedent(content))


def _build_acme_layout(root):
    """Build a synthetic Acme-like repo with the exact classes that
    confused Claude last time.
    """
    _write(root, "src/main/java/com/acme/common/Daos.java", """
        package com.acme.common;
        import com.acme.service.daos.UserDaoImpl;
        import com.acme.service.daos.ProcessedIdpEventDaoImpl;
        public class Daos {
            public UserDaoImpl getUserDao() { return null; }
            public ProcessedIdpEventDaoImpl getProcessedIdpEventDao() { return null; }
        }
    """)
    _write(root, "src/main/java/com/acme/service/daos/UserDaoImpl.java", """
        package com.acme.service.daos;
        public class UserDaoImpl {
            public java.util.List fetchByEmail(String email) { return null; }
            public void update(Object user) { }
        }
    """)
    _write(root, "src/main/java/com/acme/service/daos/ProcessedIdpEventDaoImpl.java", """
        package com.acme.service.daos;
        public class ProcessedIdpEventDaoImpl {
            public void insert(Object entity) { }
        }
    """)
    _write(root, "src/main/java/com/acme/idp/DeactivationCommand.java", """
        package com.acme.idp;
        public class DeactivationCommand {
            public String getSubjectKey() { return null; }
            public Reason getReason() { return null; }
            public SourceIdp getSourceIdp() { return null; }
            public enum Reason {
                /** User's account deactivated */
                ACCOUNT_DEACTIVATED,
                /** User unassigned from app */
                APP_UNASSIGNED
            }
            public enum SourceIdp { OKTA, AUTH0 }
        }
    """)


def test_transitive_resolution_finds_userdaoimpl(tmp_path):
    """Source imports Daos; Daos imports UserDaoImpl. UserDaoImpl must
    appear in the resolved output so Claude knows the correct package.
    """
    root = str(tmp_path)
    _build_acme_layout(root)

    source = textwrap.dedent("""
        package com.acme.idp;
        import com.acme.common.Daos;
        public class UserDeactivationService {
            private final Daos daos;
        }
    """)

    resolved = resolve_imports(
        source,
        "src/main/java/com/acme/idp/UserDeactivationService.java",
        root,
    )
    fqns = [r.fqn for r in resolved]
    assert "com.acme.common.Daos" in fqns
    assert "com.acme.service.daos.UserDaoImpl" in fqns, (
        f"UserDaoImpl not resolved transitively. Got: {fqns}"
    )
    assert "com.acme.service.daos.ProcessedIdpEventDaoImpl" in fqns


def test_same_package_files_included_without_import(tmp_path):
    """DeactivationCommand is in the same package as the source and is
    used without an explicit import. It must still be found.
    """
    root = str(tmp_path)
    _build_acme_layout(root)

    source = textwrap.dedent("""
        package com.acme.idp;
        import com.acme.common.Daos;
        public class UserDeactivationService {
            public void deactivate(DeactivationCommand cmd) { }
        }
    """)

    resolved = resolve_imports(
        source,
        "src/main/java/com/acme/idp/UserDeactivationService.java",
        root,
    )
    fqns = [r.fqn for r in resolved]
    assert "com.acme.idp.DeactivationCommand" in fqns, (
        f"Same-package DeactivationCommand not resolved. Got: {fqns}"
    )


def test_inner_enum_values_are_captured(tmp_path):
    """When a class has inner enums, ALL constant values must appear
    in the signature — not just the first one. This was the exact bug:
    Claude wrote thenReturn(Object) because it never saw Reason's values.
    """
    root = str(tmp_path)
    _build_acme_layout(root)

    source = textwrap.dedent("""
        package com.acme.idp;
        public class UserDeactivationService {
            public void deactivate(DeactivationCommand cmd) { }
        }
    """)

    resolved = resolve_imports(
        source,
        "src/main/java/com/acme/idp/UserDeactivationService.java",
        root,
    )
    cmd_sig = next(
        (r.signature for r in resolved if "DeactivationCommand" in r.fqn),
        None,
    )
    assert cmd_sig is not None
    # Both Reason values must appear
    assert "ACCOUNT_DEACTIVATED" in cmd_sig, cmd_sig
    assert "APP_UNASSIGNED" in cmd_sig, cmd_sig
    # Both SourceIdp values must appear
    assert "OKTA" in cmd_sig, cmd_sig
    assert "AUTH0" in cmd_sig, cmd_sig
    # And enum declarations should be present
    assert "enum Reason" in cmd_sig
    assert "enum SourceIdp" in cmd_sig


def test_transitive_depth_capped(tmp_path):
    """Transitive resolution stops at depth 1 to avoid recursively
    resolving the whole codebase.
    """
    root = str(tmp_path)
    # A imports B, B imports C, C imports D
    _write(root, "src/main/java/com/acme/pkg/A.java", """
        package com.acme.pkg;
        import com.acme.pkg.B;
        public class A { }
    """)
    _write(root, "src/main/java/com/acme/pkg/B.java", """
        package com.acme.pkg;
        import com.acme.pkg.C;
        public class B { }
    """)
    _write(root, "src/main/java/com/acme/pkg/C.java", """
        package com.acme.pkg;
        import com.acme.pkg.D;
        public class C { }
    """)
    _write(root, "src/main/java/com/acme/pkg/D.java", """
        package com.acme.pkg;
        public class D { }
    """)

    source = textwrap.dedent("""
        package com.acme.other;
        import com.acme.pkg.A;
        public class MyService { }
    """)

    resolved = resolve_imports(
        source, "src/main/java/com/acme/other/MyService.java", root
    )
    fqns = [r.fqn for r in resolved]
    # A is direct, B is transitive-depth-1 — both should appear
    assert "com.acme.pkg.A" in fqns
    assert "com.acme.pkg.B" in fqns
    # C requires depth 2 — must NOT appear (depth cap)
    assert "com.acme.pkg.C" not in fqns
    # D is even deeper — must not appear
    assert "com.acme.pkg.D" not in fqns
