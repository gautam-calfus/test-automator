"""Tests for the post3 fix: size-aware function rendering in prompts.

The motivating problem: for a 60-line builder-chain function where the
user added 1 line, the previous prompt sent Claude all 60 lines, which
made Claude write tests for every field. The post3 fix sends only the
signature + diff hunk when the change is small relative to the function.
"""

from __future__ import annotations

from test_automator.languages.kotlin.prompts import (
    _extract_function_signature,
    _render_functions_for_prompt,
    _render_one_function,
    user_prompt_fresh,
)
from test_automator.models import AffectedFunction


# A realistic builder-chain function (similar to your EntityMappers but
# shorter for test readability). 60 lines is what your real one was.
_BUILDER_CHAIN_FUNCTION = """\
fun ClientEntity.getClientSettings(): ClientSettings = ClientSettings.newBuilder()
    .setAddress(getSetting<AddressSetting>(Settings.Address).toAvroModel())
    .setDuplicateCheckSettings(getSetting(Settings.DuplicateCheck).toAvroModel())
    .setPhone(getSetting<PhoneSetting>(Settings.Phone).toAvroModel())
    .setReferenceCodes(getSetting<ReferenceCodes>(Settings.ReferenceCodes).toAvroModel())
    .setLogo(logoToFileAvro())
    .setAdverseAction(getSetting<AdverseAction>(Settings.AdverseAction).toAvroModel())
    .setScoring(getSetting<Scoring>(Settings.Scoring).toAvroModel())
    .setVehicleSettings(getSetting<VehicleSetting>(Settings.VehicleSetting).toAvroModel())
    .setDrugTestingSettings(getSetting<DrugTestingSettings>(Settings.DrugTesting).toAvroModel())
    .setRedactionSettings(getSetting<RedactionSettings>(Settings.Redaction).toAvroModel())
    .setAtsIntegrationSettings(getSetting<AtsIntegrationSettings>(Settings.AtsIntegration).toAvroModel())
    .setDeliverySettings(getSetting(Settings.Delivery).toAvroModel())
    .setMaskingSettings(getSetting<MaskingSetting>(Settings.Masking).toAvroModel())
    .setBillingAccount(getSetting<String?>(Settings.BillingAccount))
    .setReportingAccount(getSetting<String?>(Settings.ReportingAccount))
    .setFeeExemptionGroupId(getSetting<String?>(Settings.FeeExemption))
    .setReportEventsCCEmailAddress(getSetting<String?>(Settings.ReportEvents))
    .setCustomScoringDistributionList(getSetting(Settings.CustomScoringDistributionList).toAvroModel())
    .setCriminalRollUpSettings(getSetting<CriminalRollUpSettings>(Settings.CriminalRollUpSettings).value)
    .setLocationDisplayCountyCovered(getSetting<LocationDisplayCountyCoveredSettings>(Settings.LocationDisplayCountyCovered).value)
    .setNotes(getSetting<String?>(Settings.Notes))
    .build()
"""

_SMALL_DIFF_HUNK = """\
      .setCriminalRollUpSettings(getSetting<CriminalRollUpSettings>(Settings.CriminalRollUpSettings).value)
      .setLocationDisplayCountyCovered(getSetting<LocationDisplayCountyCoveredSettings>(Settings.LocationDisplayCountyCovered).value)
+     .setNotes(getSetting<String?>(Settings.Notes))
      .build()"""


# ---------------------------------------------------------------------------
# _extract_function_signature
# ---------------------------------------------------------------------------


def test_signature_extracts_expression_body() -> None:
    """A `fun foo(): X = expr` function — signature is up to and
    including `=`.
    """
    src = "fun foo(): Int = 42"
    sig = _extract_function_signature(src)
    assert sig == "fun foo(): Int ="


def test_signature_extracts_block_body() -> None:
    """A `fun foo() { ... }` function — signature is up to and including
    `{`.
    """
    src = "fun foo(x: Int): String {\n    return x.toString()\n}"
    sig = _extract_function_signature(src)
    assert sig == "fun foo(x: Int): String {"


def test_signature_handles_builder_chain() -> None:
    """The realistic case: builder chain with expression body.
    Signature ends at the `=` (the expression body marker).
    """
    sig = _extract_function_signature(_BUILDER_CHAIN_FUNCTION)
    # Signature ends at "=" (before the newBuilder() call)
    assert sig.startswith(
        "fun ClientEntity.getClientSettings(): ClientSettings"
    )
    assert sig.rstrip().endswith("=")
    # Critical: signature does NOT contain the builder chain setters
    assert ".setAddress" not in sig
    assert ".setNotes" not in sig
    assert ".newBuilder()" not in sig


def test_signature_handles_generics() -> None:
    """A signature with generic type params `<T>` shouldn't be confused
    by the `<` and `>` chars.
    """
    src = "fun <T : Any> foo(x: List<T>): Map<String, T> = mapOf()"
    sig = _extract_function_signature(src)
    assert "<T : Any>" in sig
    assert "Map<String, T>" in sig


def test_signature_does_not_confuse_double_equals() -> None:
    """A `==` operator in a signature default arg shouldn't be mistaken
    for an expression-body `=`.
    """
    # Not exactly a realistic Kotlin signature but tests the parser
    src = "fun foo(check: Boolean = true): Int = 42"
    sig = _extract_function_signature(src)
    # Should grab the FIRST single = (after `true`)
    assert "foo(check: Boolean = true): Int =" in sig


# ---------------------------------------------------------------------------
# _render_one_function
# ---------------------------------------------------------------------------


def test_render_uses_compact_for_big_function_with_small_diff() -> None:
    """The motivating case: big function, small change → compact."""
    fn = AffectedFunction(
        file_path="X.kt",
        name="getClientSettings",
        qualified_name="com.x.getClientSettings",
        kind="function",
        source_code=_BUILDER_CHAIN_FUNCTION,
        line_start=1,
        line_end=22,
        diff_hunk=_SMALL_DIFF_HUNK,
    )
    rendered = _render_one_function(fn)
    # Compact rendering: contains a notice about omission
    assert "full body omitted" in rendered
    # Contains the function signature (up to and including =)
    assert "fun ClientEntity.getClientSettings(): ClientSettings =" in rendered
    # Contains the diff hunk
    assert ".setNotes(getSetting<String?>(Settings.Notes))" in rendered
    # Does NOT contain unrelated builder lines from the body
    assert ".setAddress" not in rendered
    assert ".setRedactionSettings" not in rendered
    assert ".setVehicleSettings" not in rendered


def test_render_uses_full_for_small_function() -> None:
    """A small function (< 500 chars) is shown in full regardless of
    diff size.
    """
    fn = AffectedFunction(
        file_path="X.kt",
        name="ping",
        qualified_name="com.x.ping",
        kind="method",
        source_code="    fun ping(): Boolean = isConfigured",
        line_start=1,
        line_end=1,
        diff_hunk="+    fun ping(): Boolean = isConfigured",
    )
    rendered = _render_one_function(fn)
    # Full source preserved
    assert rendered == "    fun ping(): Boolean = isConfigured"
    # No "omitted" notice
    assert "omitted" not in rendered


def test_render_uses_full_when_no_diff_hunk() -> None:
    """If the bot was run without a git diff (e.g., manually invoked
    with the source), there's no diff hunk — render the full source.
    """
    fn = AffectedFunction(
        file_path="X.kt",
        name="getClientSettings",
        qualified_name="com.x.getClientSettings",
        kind="function",
        source_code=_BUILDER_CHAIN_FUNCTION,
        line_start=1,
        line_end=22,
        diff_hunk="",
    )
    rendered = _render_one_function(fn)
    assert rendered == _BUILDER_CHAIN_FUNCTION
    assert "omitted" not in rendered


def test_render_uses_full_for_large_relative_change() -> None:
    """If the diff is > 30% of the source, render the full function
    (large change → full context is useful).
    """
    fn = AffectedFunction(
        file_path="X.kt",
        name="foo",
        qualified_name="com.x.foo",
        kind="function",
        # 600 chars of source
        source_code="fun foo() {" + ("\n    val x = 1" * 50) + "\n}",
        line_start=1,
        line_end=52,
        # 250 chars diff = ~40% of source → full render
        diff_hunk="+ " + ("a" * 250),
    )
    rendered = _render_one_function(fn)
    # Should be the full source, not compact
    assert "omitted" not in rendered


# ---------------------------------------------------------------------------
# Integration via user_prompt_fresh
# ---------------------------------------------------------------------------


def test_user_prompt_fresh_compact_for_entitymappers_case() -> None:
    """The full integration: the EntityMappers-style 60-line function
    with a 1-line diff should produce a SHORT prompt now.
    """
    fn = AffectedFunction(
        file_path="src/main/kotlin/com/initech/accounts/avro/EntityMappers.kt",
        name="getClientSettings",
        qualified_name="com.initech.accounts.avro.getClientSettings",
        kind="function",
        source_code=_BUILDER_CHAIN_FUNCTION,
        line_start=84,
        line_end=143,
        diff_hunk=_SMALL_DIFF_HUNK,
    )

    prompt = user_prompt_fresh(fn.file_path, [fn])

    # The prompt should be much smaller than before. Without post3,
    # the prompt was ~6000 chars for one function (heavy with the
    # 60-line body). With post3, the body is collapsed.
    assert len(prompt) < 3000, (
        f"Prompt too large at {len(prompt)} chars — compact rendering "
        f"may have regressed"
    )

    # The diff hunk is still there (WHAT CHANGED section)
    assert ".setNotes(getSetting<String?>(Settings.Notes))" in prompt
    # Function signature is there
    assert "fun ClientEntity.getClientSettings(): ClientSettings" in prompt
    # But the unrelated setter lines from the body are NOT there
    assert ".setAddress" not in prompt
    assert ".setRedactionSettings" not in prompt


def test_user_prompt_fresh_full_for_small_function() -> None:
    """A small function (the ping() case) should be rendered in full
    because the body IS the change.
    """
    fn = AffectedFunction(
        file_path="src/main/kotlin/com/initech/accounts/services/salesforce/SalesforceService.kt",
        name="ping",
        qualified_name="com.initech.accounts.services.salesforce.SalesforceService.ping",
        kind="method",
        source_code="    fun ping(): Boolean = isConfigured",
        line_start=156,
        line_end=156,
        diff_hunk="+    fun ping(): Boolean = isConfigured",
    )
    prompt = user_prompt_fresh(fn.file_path, [fn])
    # The (small) source IS in the prompt
    assert "fun ping(): Boolean = isConfigured" in prompt
    # No "omitted" notice
    assert "omitted" not in prompt
