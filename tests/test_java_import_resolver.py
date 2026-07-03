"""Tests for v0.3.0a6: Java import resolver.

These are the exact bug scenarios Gautam reported from Acme:
- Claude invented package ``com.acme.dao.Daos`` when real is ``com.acme.common.Daos``
- Claude added ``Impl`` to ``ProcessedIdpEventDao`` (which has no Impl)
- Claude used enum value ``Reason.DEPROVISION`` when real values are
  ``ACCOUNT_DEACTIVATED`` and ``APP_UNASSIGNED``

Every test here corresponds to one of those bug classes. If they pass,
Claude will have the correct information in the prompt and shouldn't
invent these anymore.
"""

from __future__ import annotations

import os
import textwrap

import pytest

from pr_test_automator_local.languages.java.import_resolver import (
    ResolvedImport,
    format_resolved_imports_for_prompt,
    resolve_imports,
)


def _build_acme_repo(root):
    """Set up a synthetic Acme-like repo layout for testing."""
    os.makedirs(f"{root}/src/main/java/com/acme/common", exist_ok=True)
    os.makedirs(f"{root}/src/main/java/com/acme/service/daos", exist_ok=True)
    os.makedirs(f"{root}/src/main/java/com/acme/dao", exist_ok=True)
    os.makedirs(f"{root}/src/main/java/com/acme/idp", exist_ok=True)
    os.makedirs(f"{root}/src/main/java/com/acme/service", exist_ok=True)

    # The actual Daos aggregator
    open(f"{root}/src/main/java/com/acme/common/Daos.java", "w").write(
        textwrap.dedent("""\
            package com.acme.common;

            public class Daos {
                private UserDaoImpl userDao;
                private ProcessedIdpEventDao processedIdpEventDao;

                public UserDaoImpl getUserDao() { return userDao; }
                public ProcessedIdpEventDao getProcessedIdpEventDao() {
                    return processedIdpEventDao;
                }
            }
        """)
    )

    # UserDaoImpl (has an Impl)
    open(f"{root}/src/main/java/com/acme/service/daos/UserDaoImpl.java", "w").write(
        textwrap.dedent("""\
            package com.acme.service.daos;

            public class UserDaoImpl {
                public User fetchById(String id) { return null; }
                public void update(User user) {}
            }
        """)
    )

    # ProcessedIdpEventDao (NO Impl)
    open(f"{root}/src/main/java/com/acme/dao/ProcessedIdpEventDao.java", "w").write(
        textwrap.dedent("""\
            package com.acme.dao;

            public interface ProcessedIdpEventDao {
                void insert(String eventId);
            }
        """)
    )

    # Reason enum (the one Claude invented DEPROVISION for)
    open(f"{root}/src/main/java/com/acme/idp/Reason.java", "w").write(
        textwrap.dedent("""\
            package com.acme.idp;

            public enum Reason {
                /** User's account was deactivated. */
                ACCOUNT_DEACTIVATED,
                /** User was unassigned from Acme app. */
                APP_UNASSIGNED
            }
        """)
    )


def test_resolves_daos_import_with_correct_package(tmp_path):
    """The exact bug: Claude wrote ``import com.acme.dao.Daos`` when
    the real package is ``com.acme.common.Daos``. The resolver
    should find the file at the CORRECT package location.
    """
    root = str(tmp_path)
    _build_acme_repo(root)

    source = textwrap.dedent("""\
        package com.acme.service;

        import com.acme.common.Daos;

        public class MyService {
            private final Daos daos;
        }
    """)

    resolved = resolve_imports(source, "src/main/java/com/acme/service/MyService.java", root)

    daos_import = next((r for r in resolved if "Daos" in r.fqn), None)
    assert daos_import is not None, "Failed to resolve com.acme.common.Daos"
    assert daos_import.fqn == "com.acme.common.Daos"
    assert "com/acme/common/Daos.java" in daos_import.file_path.replace("\\", "/")


def test_reason_enum_values_are_extracted(tmp_path):
    """The exact bug: Claude invented ``Reason.DEPROVISION``. Real values
    are ``ACCOUNT_DEACTIVATED`` and ``APP_UNASSIGNED``. The resolver must
    include those actual values in the signature block so Claude can see them.
    """
    root = str(tmp_path)
    _build_acme_repo(root)

    source = textwrap.dedent("""\
        package com.acme.service;

        import com.acme.idp.Reason;

        public class MyService {
            void doWork() { Reason r = Reason.ACCOUNT_DEACTIVATED; }
        }
    """)

    resolved = resolve_imports(source, "src/main/java/com/acme/service/MyService.java", root)

    reason = next((r for r in resolved if r.fqn.endswith("Reason")), None)
    assert reason is not None, "Failed to resolve Reason enum"
    assert "ACCOUNT_DEACTIVATED" in reason.signature, (
        f"Expected ACCOUNT_DEACTIVATED in signature, got:\n{reason.signature}"
    )
    assert "APP_UNASSIGNED" in reason.signature
    # And, critically, no invented values
    assert "DEPROVISION" not in reason.signature


def test_processed_idp_event_dao_has_no_impl_suffix(tmp_path):
    """The exact bug: Claude wrote ``@Mock ProcessedIdpEventDaoImpl`` but
    the real class is ``ProcessedIdpEventDao`` (no Impl). The resolver
    should find the actual file and report the correct name.
    """
    root = str(tmp_path)
    _build_acme_repo(root)

    source = textwrap.dedent("""\
        package com.acme.service;

        import com.acme.dao.ProcessedIdpEventDao;

        public class MyService {
            private final ProcessedIdpEventDao dao;
        }
    """)

    resolved = resolve_imports(source, "src/main/java/com/acme/service/MyService.java", root)

    dao = next((r for r in resolved if "ProcessedIdpEvent" in r.fqn), None)
    assert dao is not None
    assert dao.fqn == "com.acme.dao.ProcessedIdpEventDao"
    # The file path should NOT have Impl in it
    assert "ProcessedIdpEventDaoImpl" not in dao.file_path
    # The signature should reference the interface, not an invented Impl
    assert "interface ProcessedIdpEventDao" in dao.signature


def test_user_dao_impl_is_found_at_correct_path(tmp_path):
    """UserDao DOES have an Impl (``UserDaoImpl``), and the resolver
    should find it at ``com.acme.service.daos.UserDaoImpl``.
    """
    root = str(tmp_path)
    _build_acme_repo(root)

    source = textwrap.dedent("""\
        package com.acme.service;

        import com.acme.service.daos.UserDaoImpl;

        public class MyService {
            private final UserDaoImpl userDao;
        }
    """)

    resolved = resolve_imports(source, "src/main/java/com/acme/service/MyService.java", root)

    user_dao = next((r for r in resolved if "UserDaoImpl" in r.fqn), None)
    assert user_dao is not None
    assert user_dao.fqn == "com.acme.service.daos.UserDaoImpl"


def test_third_party_imports_are_skipped(tmp_path):
    """Imports for classes NOT in the repo (Spring, Apache Commons, etc.)
    are silently skipped. We only care about project-internal ones —
    Claude already knows the JDK/Spring APIs.
    """
    root = str(tmp_path)
    _build_acme_repo(root)

    source = textwrap.dedent("""\
        package com.acme.service;

        import org.springframework.stereotype.Service;
        import org.slf4j.Logger;
        import com.acme.common.Daos;

        @Service
        public class MyService {
            private final Daos daos;
        }
    """)

    resolved = resolve_imports(source, "src/main/java/com/acme/service/MyService.java", root)

    fqns = [r.fqn for r in resolved]
    assert "com.acme.common.Daos" in fqns
    assert not any("springframework" in f for f in fqns)
    assert not any("slf4j" in f for f in fqns)


def test_format_for_prompt_stays_under_char_cap():
    """The formatted prompt block must not exceed the char cap even
    with many resolved imports."""
    # Build a lot of fake resolved imports
    resolved = [
        ResolvedImport(
            fqn=f"com.acme.dao.FakeDao{i}",
            file_path=f"/path/FakeDao{i}.java",
            signature="// interface com.acme.dao.FakeDao" + str(i)
                     + "\n" + "// bloat\n" * 200,
        )
        for i in range(50)
    ]

    formatted = format_resolved_imports_for_prompt(resolved, max_chars=5000)

    assert len(formatted) <= 5500  # small headroom for header/footer
    assert "omitted to stay under" in formatted


def test_no_project_prefix_returns_empty(tmp_path):
    """If the source file has no package declaration, we can't infer
    the project prefix and can't resolve any imports safely."""
    root = str(tmp_path)
    _build_acme_repo(root)

    # Note: no `package` line
    source = "import com.acme.common.Daos;\n\npublic class X {}"

    resolved = resolve_imports(source, "X.java", root)
    assert resolved == []
