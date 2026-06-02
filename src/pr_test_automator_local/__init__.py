"""PR Test Automator (Local) — generate pytest tests on your machine.

A local CLI that reads ``git diff`` against your base branch, generates
pytest tests for changed Python functions using Claude Code, optionally
commits and pushes them, and optionally opens a PR via ``gh``.

Quickstart:

    pip install -e .
    cd your-project/
    pr-test-automator-local --base-branch main

Or with all bells and whistles:

    pr-test-automator-local \
        --base-branch main \
        --commit-tests \
        --push \
        --open-pr
"""

from pr_test_automator_local.config import LocalTestConfig
from pr_test_automator_local.orchestrator import LocalTestPipeline
from pr_test_automator_local.models import (
    AffectedFunction,
    ExistingTest,
    GeneratedTest,
    PipelineResult,
    PRFile,
    PRInfo,
    StepOutcome,
    TestRunResult,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "LocalTestConfig",
    "LocalTestPipeline",
    "PipelineResult",
    "PRFile",
    "PRInfo",
    "AffectedFunction",
    "ExistingTest",
    "GeneratedTest",
    "TestRunResult",
    "StepOutcome",
]
