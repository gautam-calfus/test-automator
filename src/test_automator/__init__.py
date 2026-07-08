"""PR Test Automator (Local) — generate tests on your machine.

A local CLI that reads ``git diff`` against your base branch, generates
tests for changed source functions using Claude Code, optionally commits
and pushes them, and optionally opens a PR via ``gh``.

v0.2.0a3 ships:
- Python (full pipeline, stable since v0.1.x)
- Kotlin (parser + handler skeleton — Gradle execution and prompts come
  in later alphas)

Other languages register via
``test_automator.languages.register_language``.

Quickstart:

    pip install -e .
    cd your-project/
    test-automator --base-branch main

Or with all bells and whistles:

    test-automator \\
        --base-branch main \\
        --commit-tests \\
        --push \\
        --open-pr
"""

from test_automator.config import LocalTestConfig
from test_automator.languages import (
    KotlinLanguageHandler,
    LanguageHandler,
    PythonLanguageHandler,
    all_languages,
    register_language,
    unregister_language,
)
from test_automator.models import (
    AffectedFunction,
    ExistingTest,
    GeneratedTest,
    PipelineResult,
    PRFile,
    PRInfo,
    StepOutcome,
    TestRunResult,
)
from test_automator.orchestrator import LocalTestPipeline

__version__ = "0.1"

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
    # Language plugin API
    "LanguageHandler",
    "PythonLanguageHandler",
    "KotlinLanguageHandler",
    "register_language",
    "unregister_language",
    "all_languages",
]
