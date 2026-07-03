"""Classify Java files by category so --java-file-filter can scope the
LLM-heavy test-generation step to just services, controllers, daos, etc.

Classification is heuristic, based on filename suffix and directory path.
It's forgiving (matches naming conventions used in Acme and most other
Spring Boot Java projects) but not authoritative — a class annotated
``@Service`` in ``foo/Bar.java`` won't be caught. Rely on convention.

The rationale for filename-based classification: this runs BEFORE the
LLM sees the file, so we can't ask Claude "is this a service?" — that
would defeat the purpose (which is to save LLM calls). Filename +
directory is the practical signal.
"""

from __future__ import annotations

import os

# Categories accepted by --java-file-filter
KNOWN_CATEGORIES = ("services", "controllers", "daos", "handlers")


def classify_java_file(file_path: str) -> str | None:
    """Return the category of a Java file, or None if it doesn't fit
    a known category.

    Priority order (first match wins):
    1. Controllers — filename ends with ``Controller.java`` OR path
       contains ``/controllers/`` OR ``/controller/`` OR ``/web/``
    2. Services — filename ends with ``Service.java`` OR path contains
       ``/services/`` OR ``/service/``
    3. DAOs — filename ends with ``Dao.java``, ``DaoImpl.java``,
       ``DAO.java``, or ``DAOImpl.java`` OR path contains ``/daos/``
       OR ``/dao/``
    4. Handlers — filename ends with ``Handler.java`` OR path contains
       ``/handlers/`` OR ``/handler/``

    Files that don't fit any of these return None. Examples: DTOs,
    POJOs, exceptions, configs, entities, utils.
    """
    if not file_path.endswith(".java"):
        return None

    norm = file_path.replace("\\", "/")
    filename = os.path.basename(norm)
    stem = filename[:-5]  # strip .java

    # Controllers — check first because "SomeControllerService" would
    # otherwise match services. Highest-specificity match wins.
    if stem.endswith("Controller"):
        return "controllers"
    if "/controllers/" in norm or "/controller/" in norm or "/web/" in norm:
        return "controllers"

    # Services
    if stem.endswith("Service") or stem.endswith("ServiceImpl"):
        return "services"
    if "/services/" in norm or "/service/" in norm:
        # Filter out sub-directories that AREN'T services proper
        # (e.g. /service/permission/, /service/quartz/, /service/daos/)
        if "/service/daos/" in norm:
            return "daos"
        if "/service/permission/" in norm:
            # Permission utilities aren't really services
            return None
        if "/service/quartz/" in norm:
            # Cron job triggers — not business logic services
            return None
        if "/service/pojo/" in norm or "/pojo/" in norm:
            return None
        return "services"

    # DAOs
    if (
        stem.endswith("Dao")
        or stem.endswith("DaoImpl")
        or stem.endswith("DAO")
        or stem.endswith("DAOImpl")
    ):
        return "daos"
    if "/daos/" in norm or "/dao/" in norm:
        return "daos"

    # Handlers
    if stem.endswith("Handler"):
        return "handlers"
    if "/handlers/" in norm or "/handler/" in norm:
        return "handlers"

    return None


def should_process_java_file(
    file_path: str,
    file_filter: list[str] | None,
) -> bool:
    """Return True if the file should be processed by the test generator.

    When file_filter is None (the default), all Java files are processed.
    When file_filter is a list, only files whose classification is in
    the list are processed.

    Non-Java files are ALWAYS processed — this filter is Java-specific.
    """
    if not file_path.endswith(".java"):
        return True

    if file_filter is None:
        return True

    category = classify_java_file(file_path)
    return category in file_filter
