"""Kotlin language plugin for pr-test-automator-local.

Stage 2 of the v0.2.0 multi-language rollout: parses Kotlin source via
tree-sitter-kotlin to identify changed functions/methods.

Default conventions match Initech accounts-service:
- sources at src/main/kotlin/...
- tests at src/test/kotlin/unit/...
- test files named ``<ClassName>Tests.kt`` (plural suffix)
- assertions via Strikt (``expectThat(x).isEqualTo(y)``)
- mocks via MockK (``mockk<T>()``, ``every {} returns ...``)
- test method names as backticked English sentences

Stages 3-5 will fill in:
- Stage 3: Gradle invocation + output parsing
- Stage 4: Strikt + MockK + backticked LLM prompts
- Stage 5: polish + v0.2.0 stable release
"""

from pr_test_automator_local.languages.kotlin.handler import (
    KotlinLanguageHandler,
)

__all__ = ["KotlinLanguageHandler"]
