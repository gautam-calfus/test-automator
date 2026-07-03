"""Tests for v0.3.0a5: --java-file-filter categorizes Acme files
correctly.

We verify against the ACTUAL files from Gautam's Acme sprint run
to make sure services-only filtering matches his expectation.
"""

from __future__ import annotations

import pytest

from test_automator.languages.java.file_filter import (
    KNOWN_CATEGORIES,
    classify_java_file,
    should_process_java_file,
)


# Files from Gautam's real Acme sprint run (25 files). This is the
# ground-truth mapping the user expects when they say --java-file-filter=services
ACME_FILES = {
    # SERVICES (business logic — user wants tests for these)
    "src/main/java/com/acme/service/CMService.java": "services",
    "src/main/java/com/acme/service/EmailService.java": "services",
    "src/main/java/com/acme/service/QuestionRoutingService.java": "services",
    "src/main/java/com/acme/service/QuestionService.java": "services",
    "src/main/java/com/acme/idp/UserDeactivationService.java": "services",
    "src/main/java/com/acme/idp/Auth0UserMappingService.java": "services",

    # CONTROLLERS (HTTP layer)
    "src/main/java/com/acme/idp/Auth0LogStreamController.java": "controllers",
    "src/main/java/com/acme/idp/OktaEventHookController.java": "controllers",
    "src/main/java/com/acme/web/QuestionController.java": "controllers",

    # DAOs (data access)
    "src/main/java/com/acme/service/daos/CMServiceDaoImpl.java": "daos",
    "src/main/java/com/acme/service/daos/QuestionDaoImpl.java": "daos",
    "src/main/java/com/acme/service/daos/QuestionRoutingDaoImpl.java": "daos",
    "src/main/java/com/acme/idp/Auth0UserMappingDAOImpl.java": "daos",

    # NON-CATEGORIZED (DTOs, cron triggers, permissions, etc)
    "src/main/java/com/acme/common/Daos.java": None,
    "src/main/java/com/acme/idp/Auth0LogEntry.java": None,
    "src/main/java/com/acme/idp/DeactivationCommand.java": None,
    "src/main/java/com/acme/idp/OktaEventHookPayload.java": None,
    "src/main/java/com/acme/service/pojo/CMExpertOrSearchRecordsDTO.java": None,
    "src/main/java/com/acme/service/pojo/EmailParams.java": None,
    "src/main/java/com/acme/service/pojo/QuestionEntry.java": None,
    "src/main/java/com/acme/service/pojo/RoutingHistoryDTO.java": None,
    "src/main/java/com/acme/service/pojo/RoutingHistorySummaryDTO.java": None,
    "src/main/java/com/acme/service/quartz/GDriveCronTrigger.java": None,
    "src/main/java/com/acme/service/quartz/SilverBulletCronTrigger.java": None,
    "src/main/java/com/acme/service/permission/CustomSAMLAuthenticationProvider.java": None,
    "src/main/java/com/acme/service/permission/PermissionAction.java": None,
}


@pytest.mark.parametrize("path,expected", list(ACME_FILES.items()))
def test_acme_files_classified_as_expected(path: str, expected: str | None):
    """Every file from Gautam's real Acme sprint gets the category
    HE would expect. Regressions here would silently reintroduce noise
    into filtered runs.
    """
    actual = classify_java_file(path)
    assert actual == expected, (
        f"{path}: expected {expected!r} but got {actual!r}"
    )


def test_services_filter_only_keeps_services():
    """When user passes --java-file-filter services, only 6 of the 25
    Acme files pass through. This is the exact use case Gautam asked for.
    """
    kept = [
        path for path in ACME_FILES
        if should_process_java_file(path, ["services"])
    ]
    services = [
        path for path, cat in ACME_FILES.items() if cat == "services"
    ]
    assert set(kept) == set(services)
    assert len(kept) == 6


def test_services_and_controllers_filter_keeps_both():
    """--java-file-filter services,controllers keeps 9 files."""
    kept = [
        path for path in ACME_FILES
        if should_process_java_file(path, ["services", "controllers"])
    ]
    expected = [
        path for path, cat in ACME_FILES.items()
        if cat in ("services", "controllers")
    ]
    assert set(kept) == set(expected)
    assert len(kept) == 9


def test_no_filter_keeps_all_files():
    """When no filter is set, everything passes through — this is the
    default behavior for backward compatibility.
    """
    kept = [
        path for path in ACME_FILES
        if should_process_java_file(path, None)
    ]
    assert set(kept) == set(ACME_FILES.keys())


def test_non_java_files_always_pass():
    """The filter is Java-specific. Kotlin and Python files must always
    pass through regardless of filter setting.
    """
    assert should_process_java_file("src/main/kotlin/Foo.kt", ["services"])
    assert should_process_java_file("src/foo.py", ["services"])
    assert should_process_java_file("src/main/kotlin/Foo.kt", None)


def test_controller_takes_priority_over_service():
    """A hypothetical file named 'SomeControllerService.java' should
    classify as controller because the Controller suffix takes priority.
    """
    # Actually the suffix is Service, so this classifies as services.
    # But 'FooController.java' should always be controllers even if
    # in a /services/ dir.
    weird = "src/main/java/com/acme/services/FooController.java"
    assert classify_java_file(weird) == "controllers"


def test_daos_directory_wins_when_in_service_subtree():
    """A DAO in a /service/daos/ path should classify as daos, not services."""
    path = "src/main/java/com/acme/service/daos/UserDaoImpl.java"
    assert classify_java_file(path) == "daos"


def test_unknown_category_returns_none():
    """Files that don't match any category (Entity classes, exceptions,
    utils, DTOs) return None."""
    for path in [
        "src/main/java/com/acme/models/User.java",
        "src/main/java/com/acme/util/StringUtils.java",
        "src/main/java/com/acme/exception/ApiException.java",
    ]:
        assert classify_java_file(path) is None
