"""Resolve Java imports to on-disk source files and extract their
signatures.

The core insight this addresses: Claude generates wrong code when it
guesses at packages, class names, or enum values. Giving it the exact
class signatures from imported files eliminates the guessing.

Real user bug that motivated this module (Gautam, Acme, July 2026):

- Source imports ``com.acme.common.Daos`` — Claude wrote
  ``import com.acme.dao.Daos`` (invented package)
- Source uses ``Reason.ACCOUNT_DEACTIVATED`` — Claude wrote
  ``Reason.DEPROVISION`` (invented enum value)
- Source depends on ``ProcessedIdpEventDao`` — Claude wrote
  ``ProcessedIdpEventDaoImpl`` (invented class; the ``Impl`` doesn't exist)

All three would have been prevented by showing Claude the actual
imported files' signatures. That's what this module does.

Scope: only resolves imports that point to files IN THE REPO. Third-party
imports (Spring, Apache Commons, etc.) are skipped — Claude already
knows those. The point is to fix the project-internal blind spots.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from pr_test_automator_local.languages.java.repo_index import (
    JavaRepoIndex,
    get_repo_index,
)


# Match a Java import statement. Captures the FQN.
_IMPORT_RE = re.compile(
    r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;",
    re.MULTILINE,
)


@dataclass
class ResolvedImport:
    """One import that was resolved to a file in the repo."""

    fqn: str
    """Fully qualified name (e.g., 'com.acme.common.Daos')"""

    file_path: str
    """Absolute or repo-relative path to the .java file"""

    signature: str
    """Extracted class signature (fields + method signatures, plus
    full enum values if it's an enum). Ready to paste into a prompt."""


def resolve_imports(
    source_code: str,
    source_file_path: str,
    repo_root: str,
    max_files: int = 20,
    transitive: bool = True,
    include_same_package: bool = True,
) -> list[ResolvedImport]:
    """Given a Java source file's content and its location, find its
    project-internal imports on disk and return their extracted
    signatures.

    Args:
        source_code: full text of the source .java file
        source_file_path: path to the source file (used to derive package)
        repo_root: root of the git repo (used to search for imports)
        max_files: hard cap on imports resolved (protects against
            files that import hundreds of classes)
        transitive: if True (default), also resolve imports of resolved
            files ONE level deep.
        include_same_package: if True (default), also include ALL .java
            files in the same package as the source.

    Returns:
        List of ResolvedImport, one per import that resolved to a file
        in the repo. Third-party imports are silently skipped.
    """
    project_prefix = _guess_project_prefix(source_code)
    if not project_prefix:
        return []

    # v0.3.0a10: repo-wide file index. Walks the repo once and maps
    # every .java file's FQN → path. This replaces "guess the source
    # root" with ground truth, so multi-module repos (common/, service/)
    # and generated sources resolve correctly. If the walk fails for
    # any reason, we fall back to the conventional-roots probing.
    try:
        index: JavaRepoIndex | None = get_repo_index(repo_root)
    except Exception:
        index = None

    # Determine which getters the SOURCE FILE actually calls. If it
    # references daos.getUserDao(), we care about that getter. If it
    # doesn't touch a getter, no reason to include it in the prompt.
    # v0.3.0a8: this filter prevents the Daos signature from blowing
    # the 10K prompt cap (105 getters would truncate down to ~8 and
    # miss the ones the test actually needs).
    referenced_getters = _find_referenced_getters(source_code)

    seen_fqns: set[str] = set()
    resolved: list[ResolvedImport] = []

    def _resolve_one(fqn: str) -> str | None:
        """Look up FQN → file path, read the file, extract signature.
        Returns the file content (for transitive walks) or None."""
        if fqn in seen_fqns:
            return None
        if len(resolved) >= max_files:
            return None
        seen_fqns.add(fqn)

        file_path = _fqn_to_file_path(fqn, repo_root, index=index)
        if file_path is None:
            return None
        try:
            with open(file_path, encoding="utf-8") as fh:
                content = fh.read()
        except OSError:
            return None
        signature = _extract_signature_from_content(
            content, fqn, referenced_getters=referenced_getters,
            index=index,
        )
        if signature:
            resolved.append(
                ResolvedImport(
                    fqn=fqn,
                    file_path=file_path,
                    signature=signature,
                )
            )
        return content

    def _do_resolve(text: str, depth: int) -> None:
        if depth > 1:  # cap depth at 1 for now
            return
        imports = _parse_imports(text)
        project_imports = [
            fqn for fqn in imports if fqn.startswith(project_prefix + ".")
        ]
        # v0.3.0a10: expand project wildcard imports via the repo index.
        # jOOQ-generated DAOs are commonly pulled in with
        # ``import com.acme.domains.core.tables.daos.*;`` — the old
        # code dropped wildcards entirely, so none of those DAOs ever
        # got signatures. Only classes actually referenced in the text
        # are expanded (a package can hold hundreds of classes).
        if index is not None:
            for package in _parse_wildcard_imports(text):
                if not package.startswith(project_prefix + "."):
                    continue
                for candidate_fqn in index.fqns_in_package(package):
                    simple = candidate_fqn.rsplit(".", 1)[-1]
                    if re.search(rf"\b{re.escape(simple)}\b", text):
                        project_imports.append(candidate_fqn)
        for fqn in project_imports:
            content = _resolve_one(fqn)
            if content is not None and transitive:
                _do_resolve(content, depth + 1)

    _do_resolve(source_code, depth=0)

    # Same-package inclusion
    if include_same_package and len(resolved) < max_files:
        same_package_files = _find_same_package_files(
            source_file_path, repo_root
        )
        source_pkg = _get_source_package(source_code)
        for other_file in same_package_files:
            filename = os.path.basename(other_file)
            stem = filename[:-5] if filename.endswith(".java") else filename
            fqn = f"{source_pkg}.{stem}" if source_pkg else stem
            _resolve_one(fqn)

    return resolved


def _find_referenced_getters(source_code: str) -> set[str] | None:
    """Scan the source for ``someVariable.getXxx()`` calls and return
    the set of getter names referenced.

    Returns None if we can't reliably detect any (in which case the
    downstream extractor should include all fields, not filter).

    Real-world example: ``UserDeactivationService`` contains::

        daos.getProcessedIdpEventDao().insert(...)
        daos.getUserDao().fetchByEmail(...)

    So the returned set is ``{"getProcessedIdpEventDao", "getUserDao"}``.
    ``Daos``'s 105 fields become 2 relevant getters in the prompt.
    """
    # Match `.getXxx()` calls
    getter_calls = re.findall(r"\.(get[A-Z]\w*)\s*\(", source_code)
    if not getter_calls:
        return None  # No getter usage detected — fall back to all fields
    return set(getter_calls)


def _get_source_package(source_code: str) -> str | None:
    """Extract the source file's own full package declaration."""
    m = re.search(r"^\s*package\s+([\w.]+)\s*;", source_code, re.MULTILINE)
    return m.group(1) if m else None


def _find_same_package_files(
    source_file_path: str, repo_root: str
) -> list[str]:
    """Return other .java files in the same directory as the source.
    Excludes the source file itself.
    """
    # Resolve source directory
    if os.path.isabs(source_file_path):
        source_dir = os.path.dirname(source_file_path)
    else:
        source_dir = os.path.dirname(
            os.path.join(repo_root, source_file_path)
        )
    if not os.path.isdir(source_dir):
        return []
    source_basename = os.path.basename(source_file_path)
    result = []
    for name in sorted(os.listdir(source_dir)):
        if not name.endswith(".java"):
            continue
        if name == source_basename:
            continue
        result.append(os.path.join(source_dir, name))
    return result


def _parse_imports(source_code: str) -> list[str]:
    """Extract FQNs from all import statements. Skips wildcard imports
    (they can't be resolved to a single file).
    """
    fqns = _IMPORT_RE.findall(source_code)
    # Drop wildcards like ``com.example.foo.*``
    return [f for f in fqns if not f.endswith(".*")]


# Wildcard import: ``import com.example.foo.*;``. Captures the package.
_WILDCARD_IMPORT_RE = re.compile(
    r"^\s*import\s+(?:static\s+)?([\w.]+)\.\*\s*;",
    re.MULTILINE,
)


def _parse_wildcard_imports(source_code: str) -> list[str]:
    """Extract package names from wildcard imports."""
    return _WILDCARD_IMPORT_RE.findall(source_code)


def _guess_project_prefix(source_code: str) -> str | None:
    """Determine the project-internal package prefix from the source
    file's own package declaration. We use the first two segments —
    ``com.acme.service.CMService`` → ``com.acme``.

    Two segments is a reasonable heuristic:
    - Catches Acme's ``com.acme.*``, Initech's ``com.initech.*``,
      most enterprise Java projects
    - Doesn't over-match: won't pull in ``com.google.*`` third-party
      just because they share ``com``
    """
    match = re.search(r"^\s*package\s+([\w.]+)\s*;", source_code, re.MULTILINE)
    if not match:
        return None
    parts = match.group(1).split(".")
    if len(parts) < 2:
        return None
    return ".".join(parts[:2])


def _fqn_to_file_path(
    fqn: str, repo_root: str, index: JavaRepoIndex | None = None
) -> str | None:
    """Map ``com.acme.common.Daos`` → ``<repo>/src/main/java/com/acme/common/Daos.java``.

    Fast path: probe the conventional source roots (``src/main/java``,
    ``src/main/kotlin``, jOOQ output dirs). If none hit — multi-module
    repos, custom layouts — fall back to the repo-wide index, which
    knows the true location of every .java file (v0.3.0a10).
    """
    rel_path = fqn.replace(".", os.sep) + ".java"

    candidate_roots = [
        "src/main/java",
        "src/main/kotlin",
        "build/src/generated/java",
        "build/generated/sources/annotationProcessor/java/main",
        "src/generated/java",
    ]

    for root in candidate_roots:
        full_path = os.path.join(repo_root, root, rel_path)
        if os.path.isfile(full_path):
            return full_path

    if index is not None:
        return index.path_for_fqn(fqn)

    return None


# ---------------------------------------------------------------------------
# Signature extraction for resolved files
# ---------------------------------------------------------------------------


_CLASS_RE = re.compile(
    r"(?:^|\n)\s*(?:public\s+|private\s+|protected\s+|abstract\s+"
    r"|final\s+|static\s+)*"
    r"(class|interface|enum)\s+(\w+)",
    re.MULTILINE,
)


def _extract_signature_from_content(
    content: str,
    fqn: str,
    referenced_getters: set[str] | None = None,
    index: JavaRepoIndex | None = None,
) -> str:
    """Extract a signature summary from a Java file's content.

    For enums: extract the enum name and ALL constant values with their
    javadoc (this is the case that keeps biting us — Claude guessing
    enum values).

    For classes/interfaces: extract public/protected method signatures
    and public fields. Skip method bodies.

    Returns a compact multi-line string ready to paste into a prompt.
    """
    # Detect kind (class / interface / enum)
    kind_match = _CLASS_RE.search(content)
    if not kind_match:
        # Can't identify what this file declares — skip
        return ""

    kind = kind_match.group(1)
    class_name = kind_match.group(2)

    if kind == "enum":
        return _extract_enum_signature(content, fqn, class_name)

    return _extract_class_signature(
        content, fqn, class_name, kind,
        referenced_getters=referenced_getters, index=index,
    )


def _extract_enum_signature(content: str, fqn: str, name: str) -> str:
    """Extract enum name and all constant values.

    Enum values are the highest-value information to preserve — Claude
    inventing enum values (``Reason.DEPROVISION`` instead of
    ``Reason.ACCOUNT_DEACTIVATED``) is a top-3 bug from the field.

    We locate the enum body (between the opening ``{`` after the enum
    name and the first ``;`` or ``}``), then extract identifiers that
    look like enum constants (uppercase with underscores, or CamelCase
    identifiers followed by ``,`` / ``;`` / newline).
    """
    # Find "enum NAME {"
    enum_start = re.search(rf"\benum\s+{re.escape(name)}\s*(?:implements|\{{)", content)
    if not enum_start:
        return f"// enum {fqn} (values could not be extracted)"

    # Find the opening brace
    brace_idx = content.find("{", enum_start.start())
    if brace_idx == -1:
        return f"// enum {fqn} (values could not be extracted)"

    # Find the terminating ";" (end of enum values) OR "}" (no methods)
    body_end = _find_enum_body_end(content, brace_idx + 1)
    body = content[brace_idx + 1 : body_end]

    # Extract identifier tokens that look like enum constants
    # Enum constants must start with uppercase; we accept UPPER_SNAKE
    # or CamelCase for maximum compatibility.
    constants = re.findall(
        r"\b([A-Z][A-Z0-9_]{1,}|[A-Z][a-zA-Z0-9]*)\s*(?:\(|,|;|\n|/)",
        body,
    )
    # Deduplicate, preserve order
    seen = set()
    unique = []
    for c in constants:
        if c not in seen:
            seen.add(c)
            unique.append(c)

    if not unique:
        return f"// enum {fqn} (values could not be extracted)"

    values_str = ",\n    ".join(unique)
    return f"// enum {fqn}\npublic enum {name} {{\n    {values_str}\n}}"


def _find_enum_body_end(content: str, start: int) -> int:
    """Find the end of the enum values section: first top-level ``;`` or ``}``."""
    depth = 0
    i = start
    n = len(content)
    while i < n:
        c = content[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and c in (";", "}"):
            return i
        i += 1
    return n


def _extract_class_signature(
    content: str,
    fqn: str,
    name: str,
    kind: str,
    referenced_getters: set[str] | None = None,
    index: JavaRepoIndex | None = None,
) -> str:
    """Extract signatures for a class or interface. Handles three things:

    1. Method signatures (as before): ``public Foo bar(String x);``
    2. Inner enum declarations with all their values (v0.3.0a7)
    3. **Lombok field-based getters** (v0.3.0a8): when the class has
       ``@Data``, ``@Getter``, or field-level ``@Getter``, synthesize
       ``public <FieldType> get<FieldName>()`` entries from the field
       declarations. This is critical for Acme's ``Daos`` class,
       which is a Lombok ``@Data`` aggregator — all "methods" are
       actually generated field getters. Without this, Claude has
       no idea what ``daos.getUserDao()`` returns.

    Additionally, when synthesizing a Lombok getter, we cross-reference
    the field type against the file's own imports and emit a comment
    showing the FQN Claude should use. So the output looks like::

        // field: private final UserDaoImpl userDao;
        //   → import com.acme.service.daos.UserDaoImpl;
        public UserDaoImpl getUserDao();
    """
    class_decl_match = re.search(
        rf"\b(?:public|private|protected)?\s*(?:abstract\s+|final\s+)?"
        rf"{kind}\s+{re.escape(name)}\b[^{{]*\{{",
        content,
    )
    if not class_decl_match:
        return f"// {kind} {fqn} (declaration not found)"

    # Class declaration up to (and including) opening brace
    class_decl = class_decl_match.group(0).rstrip("{").strip()

    # Find public/protected method signatures. Match lines that start
    # with modifiers and contain a "(...)" — before the opening body
    # brace.
    method_pattern = re.compile(
        r"^\s*(?:public|protected)\s+"
        r"(?:static\s+|final\s+|abstract\s+|synchronized\s+|<[^>]+>\s+)*"
        r"[\w<>,\s\[\]?.]+?\s+"
        r"(\w+)\s*"
        r"\([^)]*\)"
        r"[^{;]*"
        r"[{;]",
        re.MULTILINE,
    )
    matches = list(method_pattern.finditer(content))

    signatures = []
    for m in matches[:30]:  # cap at 30 methods
        # Take the matched region, strip the trailing "{" or ";"
        sig = m.group(0).rstrip().rstrip("{").rstrip(";").strip()
        # Collapse internal whitespace
        sig = re.sub(r"\s+", " ", sig)
        signatures.append(f"    {sig};")

    # ------------------------------------------------------------
    # Lombok @Data / @Getter field-based getters — the fix for
    # Acme's Daos aggregator (v0.3.0a8). If the class has any of:
    #   @Data, @Getter, @Value, @AllArgsConstructor with @Getter etc.
    # then Lombok generates getters for each non-static field. We
    # synthesize those and include an FQN pointer for each field type
    # so Claude picks the right import.
    # ------------------------------------------------------------
    lombok_getters = _synthesize_lombok_getters(
        content, class_decl, referenced_getters=referenced_getters,
        own_package=fqn.rsplit(".", 1)[0] if "." in fqn else None,
        index=index,
    )
    if lombok_getters:
        if signatures:
            signatures.append("")  # separator
        signatures.extend(lombok_getters)

    # ------------------------------------------------------------
    # Inner enums — the fix for the Acme DeactivationCommand.Reason
    # / DeactivationCommand.SourceIdp bug where Claude used Object
    # because it never saw what these enums are.
    # ------------------------------------------------------------
    inner_enum_pattern = re.compile(
        r"^\s*(?:public\s+|private\s+|protected\s+|static\s+)*"
        r"enum\s+(\w+)\s*(?:implements\s+[\w.,<>\s]+\s+)?\{([^}]*)\}",
        re.MULTILINE,
    )
    inner_enums = []
    for enum_match in inner_enum_pattern.finditer(content):
        inner_name = enum_match.group(1)
        body = enum_match.group(2)
        # Skip if this is the outer declaration itself (kind == "enum")
        if kind == "enum" and inner_name == name:
            continue
        # Strip javadoc / comments so they don't confuse token parsing
        body_clean = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
        body_clean = re.sub(r"//[^\n]*", "", body_clean)
        # Enum values section ends at `;` if methods follow, else `}`
        # (we already stripped `}` in the outer pattern). Split on `;`
        # first so we drop any inner methods.
        values_section = body_clean.split(";", 1)[0]
        # Split on comma and pick tokens that look like enum constants
        raw_tokens = [t.strip() for t in values_section.split(",")]
        unique_constants: list[str] = []
        seen: set[str] = set()
        for tok in raw_tokens:
            # Constructor args if present (e.g. FOO("x")): keep just the identifier
            id_match = re.match(r"([A-Z][A-Za-z0-9_]*)", tok)
            if not id_match:
                continue
            const = id_match.group(1)
            if const not in seen:
                seen.add(const)
                unique_constants.append(const)
        if unique_constants:
            values_str = ", ".join(unique_constants)
            inner_enums.append(
                f"    // inner enum: {name}.{inner_name}\n"
                f"    public enum {inner_name} {{ {values_str} }}"
            )

    body_parts: list[str] = []
    if signatures:
        body_parts.extend(signatures)
    if inner_enums:
        if body_parts:
            body_parts.append("")  # blank line separator
        body_parts.extend(inner_enums)

    if not body_parts:
        return f"// {kind} {fqn}\n{class_decl} {{}}"

    method_block = "\n".join(body_parts)
    return (
        f"// {kind} {fqn}\n"
        f"{class_decl} {{\n"
        f"{method_block}\n"
        f"}}"
    )


def format_resolved_imports_for_prompt(
    resolved: list[ResolvedImport],
    max_chars: int = 10_000,
) -> str:
    """Render a list of resolved imports as a single prompt-friendly
    string with a hard char cap.

    The cap protects against files that import a lot — we can't just
    dump 50 signatures into every prompt. When we hit the cap, we
    include what we have and add a note.
    """
    if not resolved:
        return ""

    lines = ["// ---- Project-internal imports (actual signatures from repo) ----"]
    chars = len(lines[0])
    kept = 0
    for r in resolved:
        piece = "\n" + r.signature
        if chars + len(piece) > max_chars:
            break
        lines.append(r.signature)
        chars += len(piece)
        kept += 1

    if kept < len(resolved):
        lines.append(
            f"\n// (... {len(resolved) - kept} more import(s) omitted "
            f"to stay under {max_chars} char cap)"
        )

    return "\n\n".join(lines)


def _synthesize_lombok_getters(
    content: str,
    class_decl: str,
    referenced_getters: set[str] | None = None,
    own_package: str | None = None,
    index: JavaRepoIndex | None = None,
) -> list[str]:
    """If this class uses Lombok ``@Data`` / ``@Getter`` / ``@Value``,
    synthesize the getters Lombok would generate at compile time.

    Args:
        content: full source of the class file
        class_decl: the class declaration line (for locating body start)
        referenced_getters: if provided, only synthesize getters whose
            names appear in this set. This is critical for Acme's
            ``Daos`` class (105 fields): without filtering, the signature
            block exceeds 15KB and gets truncated in the prompt. When
            None, all fields become getters (fallback behavior).

    For each field, emit:

        // synthesized from Lombok @Data field: private final UserDaoImpl userDao;
        // field type FQN: com.acme.service.daos.UserDaoImpl
        public UserDaoImpl getUserDao();

    Real user bug this fixes: Acme's ``Daos`` is ``@Data`` with
    90+ ``private final SomeDao xxx;`` fields. Without this, Claude
    sees only the two real methods (``init()``, ``instance()``) and
    has to invent DAO types. With this, Claude gets exact return types
    AND the correct imports for the DAOs actually used by the source.
    """
    class_start = content.find(class_decl)
    if class_start == -1:
        return []
    header = content[:class_start]
    has_class_level_lombok = bool(
        re.search(r"@(?:Data|Getter|Value)\b", header)
    )
    if not has_class_level_lombok:
        return []

    imports_map = _build_import_map(content)
    wildcard_packages = _parse_wildcard_imports(content)

    field_pattern = re.compile(
        r"^\s*"
        r"(?:@\w+(?:\([^)]*\))?\s+)*"
        r"private\s+"
        r"(?:(final)\s+)?"
        r"([\w<>,\s?\[\]]+?)\s+"
        r"(\w+)\s*;",
        re.MULTILINE,
    )

    getters: list[str] = []
    seen_names: set[str] = set()
    for match in field_pattern.finditer(content):
        if match.start() < class_start:
            continue

        field_type = match.group(2).strip()
        field_name = match.group(3).strip()

        raw_field = match.group(0).strip()
        if re.search(r"\bstatic\b", raw_field):
            continue

        if field_name in seen_names:
            continue
        seen_names.add(field_name)

        getter_name = "get" + field_name[0].upper() + field_name[1:]

        # v0.3.0a8: skip getters not referenced in the source under
        # test. Without this filter, Daos (105 fields) blows the prompt
        # char cap and the getter Claude actually needs might be
        # truncated out.
        if referenced_getters is not None and getter_name not in referenced_getters:
            continue

        base_type = re.match(r"(\w+)", field_type)
        base_type_str = base_type.group(1) if base_type else field_type
        type_fqn = _resolve_type_fqn(
            base_type_str,
            imports_map,
            wildcard_packages=wildcard_packages,
            own_package=own_package,
            index=index,
        )
        fqn_comment = ""
        if type_fqn:
            fqn_comment = f"    // field type FQN: {type_fqn}\n"

        getters.append(
            f"    // synthesized from Lombok @Data field: {raw_field}\n"
            f"{fqn_comment}"
            f"    public {field_type} {getter_name}();"
        )

    return getters


def _resolve_type_fqn(
    simple_name: str,
    imports_map: dict[str, str],
    wildcard_packages: list[str],
    own_package: str | None,
    index: JavaRepoIndex | None,
) -> str | None:
    """Resolve a simple type name to its FQN, mirroring Java's own
    lookup rules for the file the type appears in:

    1. Explicit import (``import com.x.UserDaoImpl;``)
    2. Same package as the declaring file (no import needed in Java —
       this is why ``Daos.java``'s own imports were never enough)
    3. Wildcard imports (``import com.x.daos.*;``)
    4. Unique match anywhere in the repo (last resort; skipped when
       the name is ambiguous)

    Steps 2–4 need the repo index (v0.3.0a10). Before the index
    existed, only step 1 worked, so any DAO type that ``Daos.java``
    didn't explicitly import produced no FQN hint and Claude invented
    a package for it.
    """
    if simple_name in imports_map:
        return imports_map[simple_name]

    if index is None:
        return None

    if own_package:
        fqn = f"{own_package}.{simple_name}"
        if index.path_for_fqn(fqn):
            return fqn

    for package in wildcard_packages:
        fqn = f"{package}.{simple_name}"
        if index.path_for_fqn(fqn):
            return fqn

    candidates = index.fqns_for_simple_name(simple_name)
    if len(candidates) == 1:
        return candidates[0]

    return None


def _build_import_map(content: str) -> dict[str, str]:
    """Parse imports and build ``{SimpleName → FQN}``.

    Example: ``import com.acme.service.daos.UserDaoImpl;`` maps
    ``UserDaoImpl → com.acme.service.daos.UserDaoImpl``. Used to
    annotate Lombok-synthesized getters with the FQN Claude should
    use for the mock import.
    """
    imports = _IMPORT_RE.findall(content)
    result: dict[str, str] = {}
    for fqn in imports:
        simple = fqn.rsplit(".", 1)[-1]
        if simple == "*":
            continue
        result[simple] = fqn
    return result


# ---------------------------------------------------------------------------
# Post-generation import verification (v0.3.0a10)
# ---------------------------------------------------------------------------


# One import line, with its pieces: (static?, fqn). Anchored per line so
# we can rewrite the line in place while preserving formatting.
_IMPORT_LINE_RE = re.compile(
    r"^(\s*import\s+)(static\s+)?([\w.]+)(\s*;.*)$"
)


def verify_test_imports(
    test_code: str, repo_root: str
) -> tuple[str, list[str]]:
    """Validate every project-internal import in GENERATED test code
    against the repo index, and rewrite the ones Claude got wrong.

    Belt-and-braces companion to the prompt-side fixes: even with exact
    FQNs in the prompt, Claude occasionally still writes
    ``import com.acme.dao.Daos`` for ``com.acme.common.Daos``.
    Since the index knows where every class actually lives, an invented
    package whose class name is unique in the repo is mechanically
    correctable — cheaper than burning a compile + fix-loop round trip.

    Rules per import:
    - Non-project imports (JDK, Spring, Mockito...) → untouched.
    - FQN found in the repo (directly, or as an inner class / static
      member of a known class) → untouched.
    - Unknown FQN whose simple name matches exactly ONE repo class →
      rewritten to that class's real FQN (dropped instead if the
      correct import line already exists).
    - Unknown FQN with zero or multiple matches → untouched (a compile
      error here is better than a guess).

    Returns ``(corrected_code, corrections)`` where corrections are
    human-readable ``"old → new"`` strings for logging. On any index
    failure, returns the input unchanged.
    """
    project_prefix = _guess_project_prefix(test_code)
    if not project_prefix:
        return test_code, []

    try:
        index = get_repo_index(repo_root)
    except Exception:
        return test_code, []
    if not index.fqn_to_path:
        return test_code, []

    existing_imports = set(_parse_imports(test_code))
    corrections: list[str] = []
    out_lines: list[str] = []

    for line in test_code.splitlines(keepends=True):
        match = _IMPORT_LINE_RE.match(line.rstrip("\n").rstrip("\r"))
        if not match:
            out_lines.append(line)
            continue

        prefix, static_kw, fqn, suffix = match.groups()
        if not fqn.startswith(project_prefix + "."):
            out_lines.append(line)
            continue
        if index.contains_fqn_or_outer(fqn):
            out_lines.append(line)
            continue

        # Static imports name a member (``com.x.Foo.CONSTANT``) — the
        # class is the second-to-last segment. Regular imports name the
        # class directly.
        if static_kw:
            class_fqn, _, member = fqn.rpartition(".")
            simple = class_fqn.rsplit(".", 1)[-1] if "." in class_fqn else class_fqn
        else:
            member = ""
            simple = fqn.rsplit(".", 1)[-1]

        candidates = index.fqns_for_simple_name(simple)
        if len(candidates) != 1:
            out_lines.append(line)
            continue

        corrected_fqn = f"{candidates[0]}.{member}" if member else candidates[0]
        corrections.append(f"{fqn} → {corrected_fqn}")
        if corrected_fqn in existing_imports:
            # The right import is already present — drop the wrong line.
            continue
        existing_imports.add(corrected_fqn)
        newline = line[len(line.rstrip("\r\n")):]
        out_lines.append(
            f"{prefix}{static_kw or ''}{corrected_fqn}{suffix}{newline}"
        )

    return "".join(out_lines), corrections
