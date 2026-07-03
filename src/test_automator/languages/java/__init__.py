"""Java language plugin — JUnit 5 + Mockito + Maven/Gradle.

Generates JUnit 5 unit tests for Spring Boot services using:
- Mockito for collaborator mocking (``@Mock``, ``@InjectMocks``,
  ``when().thenReturn()``, ``verify()``)
- Standard JUnit assertions (``assertEquals``, ``assertThrows``, etc.)
  — NOT AssertJ
- Auto-detected build tool: ``pom.xml`` → Maven, ``build.gradle*`` → Gradle

Conventions match Acme:
- Sources at ``src/main/java/com/acme/<path>/Foo.java``
- Tests at ``src/test/java/com/acme/<path>/FooTest.java``
  (singular ``Test``, mirror package)
"""

from test_automator.languages.java.handler import JavaLanguageHandler

__all__ = ["JavaLanguageHandler"]
