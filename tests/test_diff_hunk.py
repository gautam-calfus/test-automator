"""Tests for extract_diff_hunk_for_range and the diff_hunk field on
AffectedFunction.

The motivating bug: the bot used to send Claude the entire 60-line
function body when only 1 line had changed, causing Claude to generate
27 tests and blow past the 32K output cap. The fix is to also include
a focused diff hunk so Claude focuses tests on the actual change.
"""

from __future__ import annotations

from pr_test_automator_local.languages.kotlin import prompts
from pr_test_automator_local.models import AffectedFunction
from pr_test_automator_local.utils.diff_parser import (
    extract_diff_hunk_for_range,
)


# ---------------------------------------------------------------------------
# A realistic patch matching the user's actual EntityMappers.kt change:
# one line added inside a 60-line function
# ---------------------------------------------------------------------------


REAL_ENTITY_MAPPER_PATCH = """\
diff --git a/src/main/kotlin/com/initech/accounts/avro/EntityMappers.kt b/src/main/kotlin/com/initech/accounts/avro/EntityMappers.kt
index b2ace305..af2fa10b 100644
--- a/src/main/kotlin/com/initech/accounts/avro/EntityMappers.kt
+++ b/src/main/kotlin/com/initech/accounts/avro/EntityMappers.kt
@@ -139,6 +139,7 @@ fun ClientEntity.getClientSettings(): ClientSettings = ClientSettings.newBuilder
     .setCustomScoringDistributionList(getSetting<CustomScoringDistributionListSettings>(Settings.CustomScoringDistributionList).toAvroModel())
     .setCriminalRollUpSettings(getSetting<CriminalRollUpSettings>(Settings.CriminalRollUpSettings).value)
     .setLocationDisplayCountyCovered(getSetting<LocationDisplayCountyCoveredSettings>(Settings.LocationDisplayCountyCovered).value)
+    .setNotes(getSetting<String?>(Settings.Notes))
     .build()
 
 private fun SpecialProcessHandlingSettings.toAvroModel(): SpecialProcessHandlingSettingsModels =
@@ -207,6 +208,7 @@ fun OrgUnitEntity.getOrgUnitSettings(): OrgUnitSettings = OrgUnitSettings.newBui
     .setCustomScoringDistributionList(getSetting<CustomScoringDistributionListSettings>(Settings.CustomScoringDistributionList).toAvroModel())
     .setCriminalRollUpSettings(getSetting<CriminalRollUpSettings>(Settings.CriminalRollUpSettings).value)
     .setLocationDisplayCountyCovered(getSetting<LocationDisplayCountyCoveredSettings>(Settings.LocationDisplayCountyCovered).value)
+    .setNotes(getSetting<String?>(Settings.Notes))
     .build()
"""


# ---------------------------------------------------------------------------
# extract_diff_hunk_for_range
# ---------------------------------------------------------------------------


def test_extract_hunk_isolates_one_function_from_two_function_patch() -> None:
    """The patch modifies BOTH getClientSettings (line 142) and
    getOrgUnitSettings (line 211). Asking for the range of just
    getClientSettings should return only its change, not both.
    """
    hunk = extract_diff_hunk_for_range(
        REAL_ENTITY_MAPPER_PATCH, line_start=84, line_end=143,
    )
    # Should contain the getClientSettings change
    assert "+ " in hunk
    assert "setNotes" in hunk
    # Should NOT contain the getOrgUnitSettings function declaration
    assert "getOrgUnitSettings" not in hunk


def test_extract_hunk_for_other_function() -> None:
    """Asking for getOrgUnitSettings's range should return its change,
    not getClientSettings's.
    """
    hunk = extract_diff_hunk_for_range(
        REAL_ENTITY_MAPPER_PATCH, line_start=150, line_end=212,
    )
    assert "setNotes" in hunk
    # Should NOT contain getClientSettings's declaration
    assert "getClientSettings" not in hunk


def test_extract_hunk_is_small_for_small_change() -> None:
    """For a 1-line change inside a 60-line function, the diff hunk
    should be tiny (the change line + a couple of context lines), NOT
    the whole function. This is the whole point of the fix.
    """
    hunk = extract_diff_hunk_for_range(
        REAL_ENTITY_MAPPER_PATCH, line_start=84, line_end=143,
    )
    # The full function source is ~4000 chars. The hunk should be a
    # tiny fraction of that.
    assert len(hunk) < 500, (
        f"Hunk should be small (the change + context), got {len(hunk)} "
        f"chars:\n{hunk!r}"
    )


def test_extract_hunk_includes_change_marker() -> None:
    """The hunk should mark the added line with '+' so Claude knows
    what's new vs what's existing context.
    """
    hunk = extract_diff_hunk_for_range(
        REAL_ENTITY_MAPPER_PATCH, line_start=84, line_end=143,
    )
    # The added line should start with '+'
    lines = hunk.split("\n")
    added_lines = [ln for ln in lines if ln.startswith("+")]
    assert len(added_lines) >= 1
    assert "setNotes" in added_lines[0]


def test_extract_hunk_returns_empty_for_no_changes_in_range() -> None:
    """A line range that has NO changes in the patch returns empty
    string, not an error.
    """
    hunk = extract_diff_hunk_for_range(
        REAL_ENTITY_MAPPER_PATCH, line_start=1, line_end=50,
    )
    assert hunk == ""


def test_extract_hunk_returns_empty_for_empty_patch() -> None:
    assert extract_diff_hunk_for_range("", 1, 100) == ""


def test_extract_hunk_includes_context_lines() -> None:
    """The hunk should include a few unchanged lines around each change
    for readability. Default is 2 lines on each side.
    """
    hunk = extract_diff_hunk_for_range(
        REAL_ENTITY_MAPPER_PATCH, line_start=84, line_end=143,
    )
    # We should see at least one ' ' (context) line in addition to '+'
    lines = hunk.split("\n")
    context_lines = [ln for ln in lines if ln.startswith(" ")]
    assert len(context_lines) > 0


# ---------------------------------------------------------------------------
# AffectedFunction with diff_hunk
# ---------------------------------------------------------------------------


def test_affected_function_has_default_empty_diff_hunk() -> None:
    """Backwards compatibility: callers that don't pass diff_hunk get
    an empty string default. (Python handler doesn't populate it yet.)
    """
    fn = AffectedFunction(
        file_path="x.kt",
        name="foo",
        qualified_name="x.foo",
        kind="function",
        source_code="fun foo() = 1",
        line_start=1,
        line_end=1,
    )
    assert fn.diff_hunk == ""


def test_affected_function_can_carry_diff_hunk() -> None:
    fn = AffectedFunction(
        file_path="x.kt",
        name="foo",
        qualified_name="x.foo",
        kind="function",
        source_code="fun foo() = 1",
        line_start=1,
        line_end=10,
        diff_hunk="+ added line",
    )
    assert fn.diff_hunk == "+ added line"


# ---------------------------------------------------------------------------
# Prompts now include diff_hunks
# ---------------------------------------------------------------------------


def test_user_prompt_fresh_includes_diff_hunks_section() -> None:
    """The fresh prompt template now has a 'WHAT CHANGED' section that
    embeds each affected function's diff_hunk.
    """
    fn = AffectedFunction(
        file_path="src/main/kotlin/com/initech/accounts/avro/Foo.kt",
        name="getSettings",
        qualified_name="com.initech.accounts.avro.getSettings",
        kind="function",
        source_code="fun getSettings() = Settings.newBuilder().build()",
        line_start=84,
        line_end=143,
        diff_hunk="     .setExisting(...)\n+    .setNotes(getSetting<String?>(Settings.Notes))\n     .build()",
    )

    prompt = prompts.user_prompt_fresh(
        "src/main/kotlin/com/initech/accounts/avro/Foo.kt", [fn]
    )

    # The "WHAT CHANGED" section header is present
    assert "WHAT CHANGED" in prompt
    # The diff hunk itself is in the prompt
    assert ".setNotes(getSetting<String?>(Settings.Notes))" in prompt
    # Function name appears in the diff hunk header
    assert "getSettings" in prompt
    # The full source is also there (under FULL function source)
    assert "fun getSettings() = Settings.newBuilder()" in prompt
    # And the focus-on-changes instruction
    assert "focus your tests on the changes" in prompt.lower() or \
        "focus on the changed" in prompt.lower()


def test_user_prompt_fresh_handles_missing_diff_hunk() -> None:
    """If diff_hunk is empty (e.g., handler wasn't given a patch),
    the prompt should still build successfully with a clear placeholder.
    """
    fn = AffectedFunction(
        file_path="x.kt",
        name="foo",
        qualified_name="com.foo.bar.foo",
        kind="function",
        source_code="fun foo() = 1",
        line_start=1,
        line_end=1,
        diff_hunk="",  # empty
    )
    prompt = prompts.user_prompt_fresh(
        "src/main/kotlin/com/foo/bar/X.kt", [fn]
    )
    # Should not crash. Should contain the function name and a clear
    # message that the diff hunk wasn't available.
    assert "foo" in prompt
    assert "diff hunk unavailable" in prompt.lower() or \
        "(no affected functions)" in prompt
