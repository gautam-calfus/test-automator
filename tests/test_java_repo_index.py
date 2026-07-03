"""Tests for v0.3.0a10: repo-wide Java file index.

The Acme ``Daos.java`` bug, round three. Previous fixes (v0.3.0a6–a8)
resolved imports by probing hard-coded source roots and annotated Lombok
getters using ``Daos.java``'s own explicit imports. Both fail in real
layouts:

- Multi-module repos: ``common/src/main/java/...`` never matched the
  probed roots, so ``Daos`` (and everything else) silently resolved to
  nothing.
- Same-package DAO fields: Java needs no import for same-package types,
  so the import map had no FQN for them → no ``field type FQN`` hint →
  Claude invented a package.
- Wildcard imports (jOOQ DAOs): ``import com.x.tables.daos.*;`` was
  dropped entirely.

The fix, per Gautam's suggestion: index EVERY .java file in the repo
first (FQN → path), then resolve imports against that ground truth —
and verify the generated test's imports against it afterward.
"""

from __future__ import annotations

import os
import textwrap

import pytest

from pr_test_automator_local.languages.java.import_resolver import (
    resolve_imports,
    verify_test_imports,
)
from pr_test_automator_local.languages.java.repo_index import (
    build_repo_index,
    clear_repo_index_cache,
    get_repo_index,
)


@pytest.fixture(autouse=True)
def _fresh_index_cache():
    clear_repo_index_cache()
    yield
    clear_repo_index_cache()


def _write(root: str, rel_path: str, content: str) -> str:
    path = os.path.join(root, rel_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(textwrap.dedent(content))
    return path


def _build_multimodule_acme(root: str) -> None:
    """Acme-like MULTI-MODULE layout: sources under module dirs, not
    directly under <root>/src/main/java. The old conventional-roots
    probing finds nothing here.
    """
    _write(
        root,
        "common/src/main/java/com/acme/common/Daos.java",
        """\
        package com.acme.common;

        import lombok.Data;
        import com.acme.service.daos.UserDaoImpl;

        @Data
        public class Daos {
            private final UserDaoImpl userDao;
            private final ProcessedIdpEventDao processedIdpEventDao;
        }
        """,
    )
    # Same package as Daos — no import needed in Java, so the import
    # map alone can't produce this FQN.
    _write(
        root,
        "common/src/main/java/com/acme/common/ProcessedIdpEventDao.java",
        """\
        package com.acme.common;

        public interface ProcessedIdpEventDao {
            void insert(String eventId);
        }
        """,
    )
    _write(
        root,
        "service/src/main/java/com/acme/service/daos/UserDaoImpl.java",
        """\
        package com.acme.service.daos;

        public class UserDaoImpl {
            public String fetchByEmail(String email) { return null; }
        }
        """,
    )


def test_index_maps_fqns_across_modules(tmp_path):
    root = str(tmp_path)
    _build_multimodule_acme(root)

    index = build_repo_index(root)

    assert index.path_for_fqn("com.acme.common.Daos") is not None
    assert index.path_for_fqn(
        "com.acme.service.daos.UserDaoImpl"
    ) is not None
    assert index.fqns_for_simple_name("Daos") == ["com.acme.common.Daos"]
    assert "com.acme.common.ProcessedIdpEventDao" in index.fqns_in_package(
        "com.acme.common"
    )


def test_resolves_imports_in_multimodule_repo(tmp_path):
    """The headline fix: with sources under module dirs, resolution
    used to return nothing at all. Now the index finds them."""
    root = str(tmp_path)
    _build_multimodule_acme(root)

    source = textwrap.dedent("""\
        package com.acme.service;

        import com.acme.common.Daos;

        public class UserDeactivationService {
            private final Daos daos;

            void deactivate(String email) {
                daos.getUserDao().fetchByEmail(email);
                daos.getProcessedIdpEventDao().insert(email);
            }
        }
    """)

    resolved = resolve_imports(
        source,
        "service/src/main/java/com/acme/service/UserDeactivationService.java",
        root,
    )

    daos = next((r for r in resolved if r.fqn.endswith(".Daos")), None)
    assert daos is not None, "Daos not resolved in multi-module layout"
    assert daos.fqn == "com.acme.common.Daos"


def test_lombok_getter_fqn_for_same_package_field(tmp_path):
    """``ProcessedIdpEventDao`` sits in the SAME package as ``Daos`` —
    no import statement exists for it, so the FQN hint must come from
    the repo index."""
    root = str(tmp_path)
    _build_multimodule_acme(root)

    source = textwrap.dedent("""\
        package com.acme.service;

        import com.acme.common.Daos;

        public class UserDeactivationService {
            private final Daos daos;

            void deactivate(String email) {
                daos.getUserDao().fetchByEmail(email);
                daos.getProcessedIdpEventDao().insert(email);
            }
        }
    """)

    resolved = resolve_imports(
        source,
        "service/src/main/java/com/acme/service/UserDeactivationService.java",
        root,
    )

    daos = next(r for r in resolved if r.fqn.endswith(".Daos"))
    # Explicitly-imported field type: worked before, must still work
    assert "com.acme.service.daos.UserDaoImpl" in daos.signature
    # Same-package field type: the new index-based resolution
    assert "com.acme.common.ProcessedIdpEventDao" in daos.signature


def test_lombok_getter_fqn_for_wildcard_imported_field(tmp_path):
    """jOOQ pattern: Daos pulls its DAO types in via a wildcard import."""
    root = str(tmp_path)
    _write(
        root,
        "app/src/main/java/com/acme/common/Daos.java",
        """\
        package com.acme.common;

        import lombok.Data;
        import com.acme.domains.core.tables.daos.*;

        @Data
        public class Daos {
            private final QuestionDao questionDao;
        }
        """,
    )
    _write(
        root,
        "domains/build/src/generated/java/com/acme/domains/core/tables/daos/QuestionDao.java",
        """\
        package com.acme.domains.core.tables.daos;

        public class QuestionDao {
            public String fetchById(String id) { return null; }
        }
        """,
    )

    source = textwrap.dedent("""\
        package com.acme.service;

        import com.acme.common.Daos;

        public class QuestionService {
            private final Daos daos;
            void load(String id) { daos.getQuestionDao().fetchById(id); }
        }
    """)

    resolved = resolve_imports(
        source, "app/src/main/java/com/acme/service/QuestionService.java", root
    )

    daos = next(r for r in resolved if r.fqn.endswith(".Daos"))
    assert (
        "com.acme.domains.core.tables.daos.QuestionDao" in daos.signature
    )


def test_wildcard_project_imports_are_expanded(tmp_path):
    """A source file that wildcard-imports a project package gets
    signatures for the classes it actually references."""
    root = str(tmp_path)
    _write(
        root,
        "src/main/java/com/acme/daos/OrderDao.java",
        """\
        package com.acme.daos;

        public class OrderDao {
            public void save(String order) {}
        }
        """,
    )
    _write(
        root,
        "src/main/java/com/acme/daos/UnrelatedDao.java",
        """\
        package com.acme.daos;

        public class UnrelatedDao {
            public void nothing() {}
        }
        """,
    )

    source = textwrap.dedent("""\
        package com.acme.service;

        import com.acme.daos.*;

        public class OrderService {
            private final OrderDao orderDao;
        }
    """)

    resolved = resolve_imports(
        source, "src/main/java/com/acme/service/OrderService.java", root
    )

    fqns = [r.fqn for r in resolved]
    assert "com.acme.daos.OrderDao" in fqns
    # Not referenced in the source — must NOT be dragged into the prompt
    assert "com.acme.daos.UnrelatedDao" not in fqns


# ---------------------------------------------------------------------------
# verify_test_imports — post-generation correction
# ---------------------------------------------------------------------------


def test_verify_corrects_invented_package(tmp_path):
    """The original bug, caught after the fact: Claude writes
    ``com.acme.dao.Daos``; the real class is unique in the repo, so
    the import is rewritten."""
    root = str(tmp_path)
    _build_multimodule_acme(root)

    test_code = textwrap.dedent("""\
        package com.acme.service;

        import com.acme.dao.Daos;
        import org.junit.jupiter.api.Test;

        class UserDeactivationServiceTest {
        }
    """)

    corrected, corrections = verify_test_imports(test_code, root)

    assert "import com.acme.common.Daos;" in corrected
    assert "import com.acme.dao.Daos;" not in corrected
    assert corrections == ["com.acme.dao.Daos → com.acme.common.Daos"]
    # Non-project import untouched
    assert "import org.junit.jupiter.api.Test;" in corrected


def test_verify_leaves_valid_and_thirdparty_imports_alone(tmp_path):
    root = str(tmp_path)
    _build_multimodule_acme(root)

    test_code = textwrap.dedent("""\
        package com.acme.service;

        import com.acme.common.Daos;
        import com.acme.service.daos.UserDaoImpl;
        import org.mockito.Mock;
        import static org.mockito.Mockito.when;

        class UserDeactivationServiceTest {
        }
    """)

    corrected, corrections = verify_test_imports(test_code, root)

    assert corrected == test_code
    assert corrections == []


def test_verify_skips_ambiguous_simple_names(tmp_path):
    """Two repo classes named ``Status`` in different packages: a wrong
    import can't be corrected safely, so it's left for the compiler."""
    root = str(tmp_path)
    _write(
        root,
        "src/main/java/com/acme/a/Status.java",
        "package com.acme.a;\n\npublic enum Status { OK }\n",
    )
    _write(
        root,
        "src/main/java/com/acme/b/Status.java",
        "package com.acme.b;\n\npublic enum Status { NO }\n",
    )

    test_code = textwrap.dedent("""\
        package com.acme.service;

        import com.acme.wrong.Status;

        class FooTest {
        }
    """)

    corrected, corrections = verify_test_imports(test_code, root)

    assert corrected == test_code
    assert corrections == []


def test_verify_corrects_static_import(tmp_path):
    root = str(tmp_path)
    _build_multimodule_acme(root)

    test_code = textwrap.dedent("""\
        package com.acme.service;

        import static com.acme.dao.Daos.instance;

        class FooTest {
        }
    """)

    corrected, corrections = verify_test_imports(test_code, root)

    assert "import static com.acme.common.Daos.instance;" in corrected
    assert corrections == [
        "com.acme.dao.Daos.instance → com.acme.common.Daos.instance"
    ]


def test_verify_drops_wrong_import_when_correct_one_exists(tmp_path):
    """If Claude wrote BOTH the wrong and the right import, correcting
    the wrong one must not create a duplicate."""
    root = str(tmp_path)
    _build_multimodule_acme(root)

    test_code = textwrap.dedent("""\
        package com.acme.service;

        import com.acme.common.Daos;
        import com.acme.dao.Daos;

        class FooTest {
        }
    """)

    corrected, _ = verify_test_imports(test_code, root)

    assert corrected.count("import com.acme.common.Daos;") == 1
    assert "import com.acme.dao.Daos;" not in corrected


def test_verify_allows_inner_class_imports(tmp_path):
    """``import com.acme.common.Daos.Inner;`` — not a file on disk,
    but valid Java. Must not be flagged or rewritten."""
    root = str(tmp_path)
    _build_multimodule_acme(root)

    test_code = textwrap.dedent("""\
        package com.acme.service;

        import com.acme.common.Daos.Inner;

        class FooTest {
        }
    """)

    corrected, corrections = verify_test_imports(test_code, root)

    assert corrected == test_code
    assert corrections == []


def test_index_prefers_main_sources_over_build_copies(tmp_path):
    """Same FQN under build/ and src/main/java: main sources win."""
    root = str(tmp_path)
    _write(
        root,
        "build/generated/java/com/acme/x/Thing.java",
        "package com.acme.x;\n\npublic class Thing {}\n",
    )
    main_path = _write(
        root,
        "src/main/java/com/acme/x/Thing.java",
        "package com.acme.x;\n\npublic class Thing {}\n",
    )

    index = build_repo_index(root)

    assert index.path_for_fqn("com.acme.x.Thing") == main_path


def test_index_is_cached_per_root(tmp_path):
    root = str(tmp_path)
    _write(
        root,
        "src/main/java/com/acme/x/Thing.java",
        "package com.acme.x;\n\npublic class Thing {}\n",
    )

    first = get_repo_index(root)
    second = get_repo_index(root)

    assert first is second
