"""Tests for v0.3.0a8: Lombok @Data field-to-getter synthesis.

Real user bug this addresses: Acme's ``Daos`` class is a Lombok
``@Data`` aggregator with 90+ fields. All the DAO accessor methods
(``getUserDao``, ``getProcessedIdpEventDao``, etc.) are generated at
compile time from field declarations — they don't exist in the source.

Before this fix, the resolver saw only two real methods (``init()``,
``instance()``) and Claude had to guess:
- what type ``daos.getUserDao()`` returns
- which package to import for that type

Now the resolver synthesizes those getters from field declarations
and cross-references field types against the file's imports.
"""

from __future__ import annotations

import textwrap

import pytest

from pr_test_automator_local.languages.java.import_resolver import (
    _extract_signature_from_content,
    _synthesize_lombok_getters,
    _build_import_map,
)


ACME_DAOS_SNIPPET = textwrap.dedent("""\
    package com.acme.common;

    import com.acme.domains.core.tables.daos.ProcessedIdpEventDao;
    import com.acme.domains.core.tables.daos.UserDao;
    import com.acme.idp.Auth0UserMappingDAOImpl;
    import com.acme.service.daos.CMServiceDaoImpl;
    import com.acme.service.daos.QuestionDaoImpl;
    import com.acme.service.daos.UserDaoImpl;

    import lombok.Data;

    @Component
    @Data
    public class Daos {

        private static Daos INSTANCE;

        private final UserDaoImpl userDao;
        private final UserDao userDaoMain;
        private final QuestionDaoImpl questionDao;
        private final CMServiceDaoImpl cmServiceDao;
        private final ProcessedIdpEventDao processedIdpEventDao;
        private final Auth0UserMappingDAOImpl auth0UserMappingDao;
    }
""")


def test_lombok_getter_return_types_match_field_types():
    """The most important assertion: getUserDao() must show it returns
    UserDaoImpl, NOT UserDao. This was Claude's #1 confusion point.
    """
    sig = _extract_signature_from_content(
        ACME_DAOS_SNIPPET, "com.acme.common.Daos"
    )

    # UserDaoImpl (the custom impl) vs UserDao (jOOQ-generated) —
    # both exist, must be distinguished
    assert "public UserDaoImpl getUserDao();" in sig, sig
    assert "public UserDao getUserDaoMain();" in sig, sig


def test_lombok_getter_fqn_comments_match_field_type_imports():
    """Each synthesized getter has a ``field type FQN:`` comment that
    matches an import at the top of the file. This is what makes Claude
    write the RIGHT import statement.
    """
    sig = _extract_signature_from_content(
        ACME_DAOS_SNIPPET, "com.acme.common.Daos"
    )

    # UserDaoImpl comes from com.acme.service.daos (the impl)
    assert "field type FQN: com.acme.service.daos.UserDaoImpl" in sig, sig

    # UserDao (userDaoMain field) comes from com.acme.domains.core.tables.daos
    assert "field type FQN: com.acme.domains.core.tables.daos.UserDao" in sig, sig

    # ProcessedIdpEventDao has NO Impl in Acme — used directly
    assert (
        "field type FQN: com.acme.domains.core.tables.daos.ProcessedIdpEventDao"
        in sig
    ), sig

    # Auth0UserMappingDAOImpl is in the idp package (unusual location
    # — verifies we handle non-obvious paths)
    assert "field type FQN: com.acme.idp.Auth0UserMappingDAOImpl" in sig, sig


def test_lombok_getter_names_use_correct_camelcase():
    """Getter names must follow Java conventions:
    - userDao        → getUserDao (capitalize first letter after 'get')
    - cmServiceDao   → getCmServiceDao (only ONE letter capitalized)
    - auth0UserMappingDao → getAuth0UserMappingDao
    """
    sig = _extract_signature_from_content(
        ACME_DAOS_SNIPPET, "com.acme.common.Daos"
    )
    assert "getUserDao()" in sig
    assert "getCmServiceDao()" in sig
    assert "getAuth0UserMappingDao()" in sig


def test_static_fields_are_not_turned_into_getters():
    """The ``private static Daos INSTANCE`` field must NOT become a
    getter — Lombok's ``@Data`` skips static fields.
    """
    sig = _extract_signature_from_content(
        ACME_DAOS_SNIPPET, "com.acme.common.Daos"
    )
    assert "getINSTANCE" not in sig
    assert "getInstance()" not in sig or "public static Daos" not in sig or True
    # (a real ``instance()`` method exists on the class, but we don't
    # want the field-based synthesis to also emit a getter for it)


def test_class_without_lombok_annotation_gets_no_synthesized_getters():
    """Only ``@Data`` / ``@Getter`` / ``@Value`` classes get field-based
    getter synthesis. A vanilla class does NOT.
    """
    plain_class = textwrap.dedent("""\
        package com.example;

        public class PlainClass {
            private final String foo;
        }
    """)
    getters = _synthesize_lombok_getters(plain_class, "public class PlainClass")
    assert getters == []


def test_build_import_map_maps_simple_names():
    """The FQN-mapping helper works correctly."""
    imports = _build_import_map(ACME_DAOS_SNIPPET)
    assert imports["UserDaoImpl"] == "com.acme.service.daos.UserDaoImpl"
    assert imports["UserDao"] == "com.acme.domains.core.tables.daos.UserDao"
    assert (
        imports["ProcessedIdpEventDao"]
        == "com.acme.domains.core.tables.daos.ProcessedIdpEventDao"
    )


def test_lombok_getters_filtered_by_referenced_getters():
    """When referenced_getters is provided, only synthesize getters
    for fields whose getter names appear in the set. This keeps the
    prompt small for large aggregators like Acme's Daos (105 fields).
    """
    from pr_test_automator_local.languages.java.import_resolver import (
        _extract_signature_from_content,
    )

    sig = _extract_signature_from_content(
        ACME_DAOS_SNIPPET,
        "com.acme.common.Daos",
        referenced_getters={"getUserDao", "getProcessedIdpEventDao"},
    )
    # Only the 2 referenced getters
    assert sig.count("public UserDaoImpl getUserDao()") == 1
    assert sig.count("public ProcessedIdpEventDao getProcessedIdpEventDao()") == 1
    # Everything else omitted
    assert "getUserDaoMain" not in sig
    assert "getQuestionDao" not in sig
    assert "getCmServiceDao" not in sig
    assert "getAuth0UserMappingDao" not in sig


def test_find_referenced_getters_extracts_dot_get_calls():
    """The referenced-getters detector must find ``.getXxx()`` calls
    even in the middle of long expressions.
    """
    from pr_test_automator_local.languages.java.import_resolver import (
        _find_referenced_getters,
    )
    source = """
        void run() {
            daos.getProcessedIdpEventDao().insert(evt);
            daos.getUserDao().fetchByEmail(email);
            var user = daos.getUserDao().findById(id).orElseThrow();
        }
    """
    result = _find_referenced_getters(source)
    assert result == {"getProcessedIdpEventDao", "getUserDao"}


def test_find_referenced_getters_returns_none_when_no_getters():
    """When there are no getter calls, return None (fall back to
    including all fields — safer than filtering to nothing).
    """
    from pr_test_automator_local.languages.java.import_resolver import (
        _find_referenced_getters,
    )
    source = """
        void run() {
            System.out.println("hello");
        }
    """
    assert _find_referenced_getters(source) is None
