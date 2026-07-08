"""Prompts for Java test generation.

Three modes:
- ``FRESH``: source file changed and no existing test file exists. Generate
  the entire test file from scratch.
- ``INCREMENTAL``: an existing test file was found. Generate only the NEW
  @Test methods to splice into the existing file.
- ``FIX``: previously-generated tests failed. Rewrite the test file based
  on the test runner's error output.

All three modes share Acme's conventions:
- JUnit 5 (``@Test``, ``@BeforeEach``, ``@ExtendWith(MockitoExtension.class)``)
- Mockito (``@Mock``, ``when().thenReturn()``, ``verify()``)
- JUnit assertions (``assertEquals``, ``assertThrows``, etc. — NOT AssertJ)
- Spring services with constructor injection
- DAO aggregator pattern (e.g., ``daos.getQuestionDao().fetchById(x)``)

Test class naming: ``<SourceClass>Test`` (singular, matches Acme's
existing files).
"""

from __future__ import annotations

from test_automator.models import (
    AffectedFunction,
    ExistingTest,
    GeneratedTest,
)


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------


SYSTEM_PROMPT_FRESH = """\
You are an expert Java test engineer at Acme. Generate a JUnit 5 test
class for the source class that was changed in this PR.

== Test framework ==

- JUnit 5 (``org.junit.jupiter.api.Test``, ``@BeforeEach``)
- Mockito 5+ (``@Mock``, ``@InjectMocks``, ``when().thenReturn()``,
  ``verify()``, ``ArgumentCaptor``)
- ``@ExtendWith(MockitoExtension.class)`` on the test class
- JUnit assertions: ``assertEquals``, ``assertTrue``, ``assertFalse``,
  ``assertNull``, ``assertNotNull``, ``assertThrows``, ``assertSame``,
  ``assertArrayEquals``
- Do NOT use AssertJ (``assertThat`` from org.assertj.core.api). Stick
  with JUnit assertions for consistency.

== Acme conventions ==

- Test class name: ``<SourceClass>Test`` (singular, NOT ``Tests``)
- Test methods use descriptive names: ``void shouldDoXWhenY()`` or
  ``void testFooBar()``. Either is acceptable; pick what reads better.
- Methods are package-private (no ``public`` modifier — JUnit 5 doesn't
  require it)
- DAO aggregator pattern: services depend on a single ``Daos daos`` field
  that exposes ``daos.getQuestionDao()``, ``daos.getUserDao()``, etc.
  Acme's ``Daos`` is a Lombok ``@Data`` class — its getters are
  generated from its fields. The CLASS SIGNATURES section will show
  entries like this for each DAO on Daos:

      // synthesized from Lombok @Data field: private final UserDaoImpl userDao;
      // field type FQN: com.acme.service.daos.UserDaoImpl
      public UserDaoImpl getUserDao();

  **Use both pieces of information verbatim.** The return type
  (``UserDaoImpl``) is the type to declare for the ``@Mock`` field.
  The ``field type FQN`` line is the exact import statement you need.
  Never invent, shorten, or "correct" either one. Example:

      // Correct — matches what Daos actually exposes
      import com.acme.service.daos.UserDaoImpl;
      import com.acme.domains.core.tables.daos.ProcessedIdpEventDao;

      @Mock private Daos daos;
      @Mock private UserDaoImpl userDao;
      @Mock private ProcessedIdpEventDao processedIdpEventDao;
      @BeforeEach
      void setup() {
          when(daos.getUserDao()).thenReturn(userDao);
          when(daos.getProcessedIdpEventDao()).thenReturn(processedIdpEventDao);
      }

  Note that ``UserDao`` (with no Impl) and ``UserDaoImpl`` (with Impl)
  can BOTH exist in Acme at different packages — the ``field type
  FQN`` comment removes any ambiguity.

- **Import statements: use the EXACT package shown in CLASS SIGNATURES
  or in ``field type FQN`` comments.** Do not invent or shorten package
  names. If CLASS SIGNATURES shows ``com.acme.common.Daos``, write
  ``import com.acme.common.Daos;`` — never ``import com.acme.dao.Daos;``.

- **Class NAMES must be copied character-for-character from CLASS
  SIGNATURES / the source file.** Never change casing, add or drop an
  ``Impl`` suffix, or otherwise paraphrase a name: if the signature
  says ``Auth0UserMappingDAOImpl``, writing ``Auth0UserMappingDao``
  will not compile. If a class you want isn't shown anywhere in the
  prompt, mock it via the interface/field the source class actually
  declares instead of guessing its name.

- **Enum return types:** if a method's return type is an ``enum``,
  return the ACTUAL enum value shown in CLASS SIGNATURES (which
  includes enum constants). ``DeactivationCommand.Reason`` has values
  ``ACCOUNT_DEACTIVATED`` and ``APP_UNASSIGNED`` — do not use invented
  constants like ``DEPROVISION``. String-vs-enum mismatches will cause
  ``ClassCastException`` at runtime.

- Prefer ``lenient()`` mocks when stubbing setup calls that some tests
  might not exercise: ``lenient().when(command.getSourceIdp()).thenReturn(SourceIdp.OKTA)``.
  This avoids ``UnnecessaryStubbingException`` when the mock is used
  in a ``@BeforeEach`` but individual tests don't touch every stub.

- Mock all collaborators (other services, DAOs). Don't use ``@SpringBootTest``;
  these are unit tests, not integration tests.

== What to test ==

For the changed source methods (shown in the user prompt):
- Focus on the CHANGED behavior — the lines added/modified by the diff
- Include both happy path and edge cases
- Verify mock interactions where behavior is observable through them
  (e.g., ``verify(dao).update(captor.capture())`` then assert on the
  captured value)
- For methods with branching logic, cover each branch with at least one
  test

Do NOT generate tests for:
- Trivial getters/setters
- Constructors that just assign fields
- Comment-only changes (the diff shows context for these but they need
  no tests)
- Whitespace/formatting changes (e.g., reformatting a method signature
  across multiple lines)

== Constructor arguments — USE EXACT SIGNATURES ==

When constructing instances of the source class or calling its methods,
use the EXACT parameter list shown in CLASS SIGNATURES. Do not invent
parameter names or guess. If you need a class you've never seen before,
add the appropriate ``import`` and mock it with ``@Mock``.

== Output format — STRICT ==

Output the COMPLETE test file. Nothing else.

FORBIDDEN:
- Do NOT write any prose, explanation, or commentary before/after the code
- Do NOT use markdown fences (no triple backticks)
- Do NOT quote the source code being tested

REQUIRED:
- The FIRST line of your response must be ``package <something>;``
- The LAST line of your response must be the test class's closing ``}``
- Everything between must be valid Java source: package, imports, class
  declaration, methods.

If your response does not start with ``package`` on the first line, the
bot will reject it.
"""


SYSTEM_PROMPT_INCREMENTAL = """\
You are an expert Java test engineer at Acme. An existing JUnit 5
test file exists for the source class. Generate ONLY new ``@Test`` method
declarations to add to that file — covering the changes shown in the
PR diff.

== Test framework ==

Same as fresh-generation: JUnit 5 + Mockito + standard JUnit assertions.
Match the style of the existing test file shown in the user prompt
(field declarations, ``@BeforeEach`` setup, mock initialization).

== What to output ==

Just the new ``@Test`` methods. Do NOT include:
- ``package`` declaration
- ``import`` statements
- The test class declaration or its opening/closing braces
- Any class-level fields, ``@Mock`` declarations, ``@BeforeEach``, etc.
  (those already exist in the file)

== What to test ==

Focus on the CHANGED behavior — the lines added/modified by the diff.
Maximum 6 tests per source function. Match the style of the existing
tests in the file (especially mocking patterns and assertion choices).

When constructing instances of classes from the CLASS SIGNATURES section
of the user prompt, use the EXACT parameter list shown. Don't invent
parameters.

Copy class names character-for-character from CLASS SIGNATURES, the
source file, or the existing test file — never change casing or add or
drop an ``Impl`` suffix. Prefer classes the existing test file already
uses; if you need one it doesn't, the bot resolves its import
automatically as long as the NAME is exactly right.

== Output format — STRICT ==

Just method declarations. No prose, no fences, no class wrapper.

Example:
    @Test
    void shouldDoXWhenY() {
        // arrange
        when(dao.fetch("x")).thenReturn(...);

        // act
        var result = service.method();

        // assert
        assertEquals(..., result);
        verify(dao).update(any());
    }

    @Test
    void shouldThrowWhenZ() {
        // ...
    }
"""


SYSTEM_PROMPT_FIX = """\
You are an expert Java test engineer at Acme. A JUnit 5 test you
previously generated has FAILED when run with Maven/Gradle. Fix the
test so it passes — without modifying the source code being tested.

The user prompt will show you:
- The source file under test
- The failing test file (the one you previously generated)
- Maven/Gradle's output (compile error, assertion failure, or
  unexpected exception)

== Common failure modes ==

1. **Unresolved reference**: you used a method or field that doesn't
   exist on the source class. Look at the CLASS SIGNATURES — use only
   methods/fields shown there.

2. **Type mismatch**: read the parameter types in CLASS SIGNATURES and
   match them. ``UUID`` is not ``String``; ``int`` is not ``Integer``.

3. **Missing import**: add it. Common ones at Acme:
   - ``import org.junit.jupiter.api.Test;``
   - ``import org.junit.jupiter.api.BeforeEach;``
   - ``import org.junit.jupiter.api.extension.ExtendWith;``
   - ``import org.mockito.Mock;``
   - ``import org.mockito.InjectMocks;``
   - ``import org.mockito.junit.jupiter.MockitoExtension;``
   - ``import static org.mockito.Mockito.*;``
   - ``import static org.junit.jupiter.api.Assertions.*;``

4. **Mockito error: ``UnnecessaryStubbingException``**: you stubbed a mock
   method that wasn't called. Remove the unused stub.

5. **Mockito error: ``WrongTypeOfReturnValue``**: ``when().thenReturn()``
   types don't match. Check the DAO method's return type.

6. **NullPointerException in test**: a collaborator wasn't mocked or
   stubbed. Add ``@Mock`` for it and stub its methods.

== Output format — STRICT ==

Output the COMPLETE fixed test file. Nothing else.

FORBIDDEN:
- Do NOT write prose, explanation, or commentary
- Do NOT include phrases like "Looking at the source code" or "Here's
  the fix" or "I'll fix this by..."
- Do NOT include quoted snippets of the source code being tested
- Do NOT use markdown fences (no triple backticks)
- Do NOT write multiple code blocks — exactly ONE block, the complete
  test file

REQUIRED:
- The FIRST line of your response must be ``package <something>;``
- The LAST line of your response must be the test class's closing ``}``
- Everything between must be valid Java.

If your response does not start with ``package`` on the first line, the
bot will reject it and the fix will be lost.
"""


# ---------------------------------------------------------------------------
# User templates
# ---------------------------------------------------------------------------


_USER_TEMPLATE_FRESH = """\
Generate a JUnit 5 test class for the following source file.

Source file:    {source_file}
Source package: {source_package}

The test file will be saved at: {test_file_path}
The test class MUST be named:   {test_class_name}
Test package:                   {test_package}

== CLASS SIGNATURES in this file (USE THESE EXACTLY — do not invent parameters) ==

```java
{class_context}
```

== WHAT CHANGED in this PR ==

{diff_hunks}

== FULL function source (for context only — focus your tests on the
changes above, not on testing the whole method exhaustively) ==

```java
{functions_code}
```

Generate the complete test file. Follow the style guide in the system
prompt exactly. Focus on the CHANGED lines, not the full methods.
When constructing instances of the classes above, USE THE EXACT
CONSTRUCTOR SIGNATURE shown in the CLASS SIGNATURES section — do not
invent parameter names. Output ONLY Java code, no markdown fences,
no commentary, no leading or trailing prose.
"""


_USER_TEMPLATE_INCREMENTAL = """\
Add new ``@Test`` methods to an existing JUnit 5 test file.

Source file:           {source_file}
Test file (existing):  {test_file_path}

== CLASS SIGNATURES in this file (USE THESE EXACTLY — do not invent parameters) ==

```java
{class_context}
```

== WHAT CHANGED in this PR ==

{diff_hunks}

== Source methods for context (focus tests on the CHANGES above) ==

```java
{functions_code}
```

== Existing test file ==

```java
{existing_content}
```

Generate ONLY the new ``@Test`` method declarations to add to the file.
Focus on the CHANGED lines (the "WHAT CHANGED" section above) — do not
write tests for code that wasn't changed. Maximum 6 tests per source
method. Match the style of the existing tests above. Reuse the
class-level mocks and ``@BeforeEach`` setup that are already declared.
Do not write a package declaration, imports, or class wrapper — those
already exist.
"""


_USER_TEMPLATE_FIX = """\
A previously-generated JUnit 5 test file is failing. Fix it.

Source file under test: {source_file}
Test file:              {test_file_path}

== Source file content ==

```java
{source_code}
```

== Current (failing) test file ==

```java
{test_code}
```

== Test runner output (Maven/Gradle) ==

```
{runner_output}
```

Rewrite the COMPLETE test file so the failures are fixed. Do NOT modify
the source code — only the test code. Keep the test class name and
package declaration the same. Output ONLY the fixed Java file, no
markdown fences, no prose.
"""


# ---------------------------------------------------------------------------
# Builder functions
# ---------------------------------------------------------------------------


def user_prompt_fresh(
    source_path: str, affected: list[AffectedFunction]
) -> str:
    """Build the user prompt for fresh test-file generation.

    Derives source package, test file path, test class name, and test
    package from the source path. For Acme: source at
    ``src/main/java/com/acme/x/Foo.java`` →
    test at ``src/test/java/com/acme/x/FooTest.java``
    in package ``com.acme.x`` with class ``FooTest``.
    """
    source_package = _derive_source_package(source_path)
    test_package = source_package  # Java tests mirror source packages
    test_file_path = _derive_test_file_path(source_path)
    test_class_name = _derive_test_class_name(source_path)

    functions_code = _render_functions_for_prompt(affected)
    diff_hunks = _format_diff_hunks(affected)
    class_context = _format_class_context(affected)

    return _USER_TEMPLATE_FRESH.format(
        source_file=source_path,
        source_package=source_package,
        test_file_path=test_file_path,
        test_class_name=test_class_name,
        test_package=test_package,
        class_context=class_context,
        diff_hunks=diff_hunks,
        functions_code=functions_code,
    )


def user_prompt_incremental(
    source_path: str,
    existing: ExistingTest,
    affected: list[AffectedFunction],
    trimmed_existing_content: str = "",
    removed_tests_code: str = "",
) -> str:
    """Build the user prompt for incremental merge.

    For Acme this path is rarely hit (no existing tests). When it
    IS hit, this prompt asks Claude to write JUST new @Test methods
    that get spliced into the existing file by the merger.
    """
    functions_code = _render_functions_for_prompt(affected)
    diff_hunks = _format_diff_hunks(affected)
    class_context = _format_class_context(affected)
    existing_content = trimmed_existing_content or (existing.content or "").strip()

    return _USER_TEMPLATE_INCREMENTAL.format(
        source_file=source_path,
        test_file_path=existing.test_file_path,
        class_context=class_context,
        diff_hunks=diff_hunks,
        functions_code=functions_code,
        existing_content=existing_content,
    )


# ---------------------------------------------------------------------------
# Test-path / package derivation
# ---------------------------------------------------------------------------


def _derive_source_package(source_path: str) -> str:
    """Extract Java package from a path under ``src/main/java/``.

    Example: ``src/main/java/com/acme/service/CMService.java`` →
    ``com.acme.service``. Falls back to empty string if path
    doesn't match the convention.
    """
    norm = source_path.replace("\\", "/")
    idx = norm.find("src/main/java/")
    if idx == -1:
        return ""
    rest = norm[idx + len("src/main/java/"):]
    # Drop filename
    dir_part = rest.rsplit("/", 1)[0] if "/" in rest else ""
    return dir_part.replace("/", ".")


def _derive_test_file_path(source_path: str) -> str:
    """Map source → test path: src/main/java/X.java → src/test/java/XTest.java."""
    norm = source_path.replace("\\", "/")
    # Swap src/main/java with src/test/java
    if "/src/main/java/" in norm:
        test_dir = norm.replace("/src/main/java/", "/src/test/java/", 1)
    elif norm.startswith("src/main/java/"):
        test_dir = norm.replace("src/main/java/", "src/test/java/", 1)
    else:
        test_dir = norm
    # Append Test suffix to filename stem
    dir_part, filename = test_dir.rsplit("/", 1) if "/" in test_dir else ("", test_dir)
    stem, ext = filename.rsplit(".", 1) if "." in filename else (filename, "java")
    test_filename = f"{stem}Test.{ext}"
    return f"{dir_part}/{test_filename}" if dir_part else test_filename


def _derive_test_class_name(source_path: str) -> str:
    """``Foo.java`` → ``FooTest``."""
    base = source_path.replace("\\", "/").rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return f"{stem}Test"


def user_prompt_fix(generated: GeneratedTest, runner_output: str) -> str:
    """Build the user prompt for fix-loop attempts.

    v0.3.0a3 fix: Previously read the ENTIRE source file into the prompt.
    For CMService.java (6000+ lines, ~250K chars) this produced a
    ~280K-char prompt — one call could consume ~70K tokens, more than
    everything else in a run combined.

    Now: cap the source at 30K chars. For larger files, use class
    signature extraction (imports + class declarations + method
    signatures, but not method bodies) — the same trick fresh mode
    uses. This reduces CMService from 250K→~17K chars.

    Rationale: fix-mode needs Claude to know what classes/methods
    EXIST in the source file (so mocks and calls are correct), but
    it does NOT need every method's body. The test file itself and
    the runner output already show what's failing.
    """
    source_code = _read_source_capped(generated.source_file_path)

    return _USER_TEMPLATE_FIX.format(
        source_file=generated.source_file_path,
        test_file_path=generated.test_file_path,
        source_code=source_code,
        test_code=generated.content,
        runner_output=runner_output[:8000],  # cap to avoid blowing the prompt
    )


# Hard cap on source-file content in fix-mode prompts. If the raw source
# exceeds this, we fall back to class-signature extraction. This came
# out of a real Acme run where the uncapped fix-loop prompt was
# 278,867 chars for CMService.java.
_FIX_SOURCE_HARD_CAP = 30_000


def _read_source_capped(source_file_path: str) -> str:
    """Read source; if too large, return class signatures only.

    Returns a placeholder string if the file can't be read.
    """
    try:
        with open(source_file_path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return "(source file content unavailable)"

    if len(raw) <= _FIX_SOURCE_HARD_CAP:
        return raw

    # File too large — use class signature extraction.
    try:
        from test_automator.languages.java.analyzer import (
            extract_class_signatures,
        )
        compact = extract_class_signatures(raw, compact=True)
        prefix = (
            f"// NOTE: full source is {len(raw)} chars — showing compact\n"
            f"// class signatures only (method bodies omitted). If you\n"
            f"// need to see a specific method body, reference it by name\n"
            f"// from the test code and runner output.\n\n"
        )
        return prefix + compact
    except Exception:
        # If signature extraction fails, fall back to a truncated view
        return raw[:_FIX_SOURCE_HARD_CAP] + (
            f"\n\n// ... (truncated; original file was {len(raw)} chars)\n"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_functions_for_prompt(affected: list[AffectedFunction]) -> str:
    """Render the affected methods for the prompt.

    For LARGE methods (>2000 chars), include just the signature plus the
    diff hunk (which is shown separately in WHAT CHANGED). For smaller
    methods, include the full source.

    This is the same logic that worked for Kotlin on EntityMappers/UserService.
    """
    if not affected:
        return "(no affected methods)"

    sections: list[str] = []
    for fn in affected:
        body = fn.source_code or ""
        # If the method is large AND the diff hunk is small, just show
        # the signature — Claude has the diff in WHAT CHANGED
        if len(body) > 2000 and fn.diff_hunk and len(fn.diff_hunk) < len(body) // 2:
            sig = _extract_method_signature(body)
            sections.append(
                f"// {fn.kind}: {fn.qualified_name} "
                f"(body omitted — {len(body)} chars; see WHAT CHANGED above)\n"
                f"{sig}"
            )
        else:
            sections.append(f"// {fn.kind}: {fn.qualified_name}\n{body}")
    return "\n\n".join(sections)


def _extract_method_signature(body: str) -> str:
    """Return everything from the start of ``body`` up to (and including)
    the opening ``{`` of the method body, then ``    // ... }``.

    Defensive: if no ``{`` is found, return the first 200 chars.
    """
    brace_idx = body.find("{")
    if brace_idx == -1:
        return body[:200] + "..."
    return body[: brace_idx + 1] + "\n    // ... body omitted\n}"


def _format_diff_hunks(affected: list[AffectedFunction]) -> str:
    if not affected:
        return "(no affected methods)"

    sections: list[str] = []
    for fn in affected:
        if fn.diff_hunk.strip():
            sections.append(
                f"--- In {fn.name} (lines {fn.line_start}-{fn.line_end}): ---\n"
                f"{fn.diff_hunk}"
            )
        else:
            sections.append(
                f"--- In {fn.name}: (diff hunk unavailable — assume the "
                f"entire method is the change) ---"
            )
    return "\n\n".join(sections)


def _format_class_context(affected: list[AffectedFunction]) -> str:
    """Return the class signatures from the source file as a single
    string for the prompt.
    """
    if not affected:
        return "(no class signatures available)"
    ctx = affected[0].class_context.strip()
    if not ctx:
        return (
            "(class signatures unavailable — fall back to inferring "
            "from the method source below)"
        )
    return ctx
