"""Declared synthetic source relations; these tests are not OCR quality scores."""
import copy

from test_document_header_association_098 import block, field, page

from trackvance.document_header_association import header_observations
from trackvance.document_header_association_v2 import header_observations_v2
from trackvance.document_lossless import _resolve


def decimal(name="subtotal", label="Subtotal", **extra):
    return field(name, label, logical_type="DECIMAL", decimal_separator=",", thousands_separator=".", decimal_format_explicit=True, **extra)


def resolved(fields, *pages):
    observations, traces = header_observations_v2(list(pages), fields)
    return {item["name"]: _resolve(observations[item["name"]], item) for item in fields}, traces


def test_adjacent_tax_caption_does_not_become_subtotal_but_is_retained():
    config = decimal()
    source = page(html="<table><tr><td>Subtotal</td><td>370.000,00</td></tr>"
        "<tr><td>IVA 19%</td><td>70.300,00</td></tr></table>")
    cells, traces = resolved([config], source)
    assert cells["subtotal"]["validity"] == "VALID"
    assert cells["subtotal"]["value"] == "370000.00"
    rejected = [item for item in traces["subtotal"] if not item["eligible_for_field"]]
    assert any(item["observed"] == "IVA 19%" and item["reason"] == "TARGET_IS_ROLE_CAPTION" for item in rejected)
    assert all(item["value_refs"] and item["region"] is None for item in rejected)


def test_transposed_certificate_table_still_uses_below_relationship():
    configs = [field("duration", "Validity duration"), field("authority", "Issuing authority")]
    cells, traces = resolved(configs, page(html="<table><tr><th>Validity duration</th><th>Issuing authority</th></tr>"
        "<tr><td>30 days</td><td>Example authority</td></tr></table>"))
    assert cells["duration"]["value"] == "30 days"
    assert cells["authority"]["value"] == "Example authority"
    assert all(item["rule"] == "DECLARED_TABLE_BELOW" for values in traces.values() for item in values)


def test_true_subtotal_contradiction_remains_ambiguous():
    cells, _ = resolved([decimal()], page(block("Subtotal: 100,00")), page(block("Subtotal: 120,00"), number=2))
    assert cells["subtotal"]["value"] is None
    assert cells["subtotal"]["validity"] == "AMBIGUOUS"
    assert {item["observed"] for item in cells["subtotal"]["candidate_alternatives"]} == {"100,00", "120,00"}


def test_invalid_literal_under_correct_caption_is_not_filtered_by_type():
    cells, traces = resolved([decimal()], page(block("Subtotal: ??")), page(block("Subtotal: 100,00"), number=2))
    assert cells["subtotal"]["validity"] == "AMBIGUOUS"
    assert any(item["observed"] == "??" and item["eligible_for_field"] for item in traces["subtotal"])


def test_balance_caption_owns_embedded_period_word():
    configs = [field("period", "Período"), decimal("opening_balance", "Saldo inicial")]
    cells, traces = resolved(configs, page(block("Período: 2026-09-01 a 2026-09-30"),
        block("Saldo al inicio del período: 2.300,00")))
    assert cells["period"]["value"] == "2026-09-01 a 2026-09-30"
    assert cells["opening_balance"]["value"] == "2300.00"
    assert any(item["raw_observed"] == "2.300,00" and not item["eligible_for_field"]
        and item["reason"] == "CAPTION_OWNED_BY_DIFFERENT_ROLE" for item in traces["period"])


def test_english_balance_caption_is_not_reporting_period_variant():
    cells, traces = resolved([field("period", "Period")], page(
        block("Opening balance for period: 80.00"), block("Reporting period: February 2026")))
    assert cells["period"]["value"] == "February 2026"
    assert any(item["observed"] == "80.00" and item["status"] == "REJECTED" for item in traces["period"])


def test_two_explicit_periods_are_a_real_conflict():
    cells, _ = resolved([field("period", "Period")], page(block("Period: January 2026")),
        page(block("Period: February 2026"), number=2))
    assert cells["period"]["validity"] == "AMBIGUOUS"


def test_unproven_embedded_relation_is_uncertainty_not_positive_rejection():
    cells, traces = resolved([field("period", "Period")], page(block("Discussed period: January 2026")))
    assert cells["period"]["validity"] == "AMBIGUOUS"
    assert traces["period"][0]["status"] == "UNCERTAIN"
    assert traces["period"][0]["eligible_for_field"] is True


def test_wrong_observed_party_section_cannot_be_overwritten_by_requested_role():
    cells, traces = resolved([field("issuer", "Vendor", section="ISSUER")], page(
        block("Buyer", [20, 100, 400, 120]), block("Vendor: Company B", [20, 140, 400, 160])))
    assert cells["issuer"]["validity"] == "MISSING"
    assert traces["issuer"][0]["source_section"] == "BUYER"
    assert traces["issuer"][0]["requested_section"] == "ISSUER"
    assert traces["issuer"][0]["reason"] == "OBSERVED_SECTION_OWNS_DIFFERENT_ROLE"


def test_unknown_geometry_cannot_bind_separate_blocks():
    observations, _ = header_observations_v2([page(block("Reference"), block("ABC"))], [field("ref", "Reference")])
    assert observations["ref"] == []


def test_native_normalized_measured_blocks_are_supported_without_pixel_claim():
    source = page(block("Reference", [.1, .1, .3, .12]), block("ABC", [.1, .13, .3, .15]))
    source["geometry"] = {"unit": "NORMALIZED", "width": 1, "height": 1}
    cells, traces = resolved([field("ref", "Reference")], source)
    assert cells["ref"]["value"] == "ABC"
    assert traces["ref"][0]["region"] == [.1, .13, .3, .15]


def test_declared_value_pattern_failure_remains_observed_and_unresolved():
    config = field("ref", "Reference", visual={"value_pattern": r"^(\d+)$"})
    cells, traces = resolved([config], page(block("Reference: ??")))
    assert cells["ref"]["validity"] == "AMBIGUOUS"
    assert traces["ref"][0]["raw_observed"] == "??"
    assert traces["ref"][0]["eligible_for_field"]


def test_v1_results_and_original_ir_are_not_reinterpreted_by_v2():
    fields = [decimal()]
    source = page(html="<table><tr><td>Subtotal</td><td>100,00</td></tr>"
        "<tr><td>IVA 19%</td><td>19,00</td></tr></table>")
    original = copy.deepcopy(source)
    before = header_observations([source], fields)
    header_observations_v2([source], fields)
    assert source == original
    assert header_observations([source], fields) == before
    assert {item[0] for item in before[0]["subtotal"]} == {"100,00", "IVA 19%"}


def test_complete_monetary_caption_with_declared_currency_suffix_needs_no_colon():
    config = field("total", "Total due", logical_type="DECIMAL", decimal_separator=".",
        decimal_format_explicit=True, currency_symbols=["USD"])
    cells, traces = resolved([config], page(block("TOTAL DUE USD 240.00")))
    assert cells["total"]["value"] == "240.00"
    assert any(item["status"] == "PROVEN" and item["reason"] == "DECLARED_MONETARY_ROLE_VALUE_SUFFIX" for item in traces["total"])


def test_monetary_caption_variant_inside_single_html_cell_keeps_logical_geometry():
    config = field("closing_balance", "Closing balance", logical_type="DECIMAL", decimal_separator=".",
        thousands_separator=",", decimal_format_explicit=True, currency_symbols=["EUR"],
        normalization={"decimal_formats": "UNAMBIGUOUS_LOCALE_V1"})
    cells, traces = resolved([config], page(html="<table><tr><td colspan='4'>BALANCE AT END OF PERIOD 3,100.00 EUR</td></tr></table>"))
    assert cells["closing_balance"]["value"] == "3100.00"
    assert all(item["geometry_kind"] == "UNKNOWN" and item["region"] is None for item in traces["closing_balance"])


def test_invalid_monetary_tail_is_retained_as_uncertainty():
    config = decimal("total", "Total due")
    cells, traces = resolved([config], page(block("Total due ??")))
    assert cells["total"]["validity"] == "AMBIGUOUS"
    assert traces["total"][0]["observed"] == "??" and traces["total"][0]["eligible_for_field"]


def test_longer_unit_price_role_cannot_become_line_amount():
    config = decimal("amount", "Amount")
    cells, traces = resolved([config], page(block("Amount per unit 12,00")))
    assert cells["amount"]["validity"] == "MISSING"
    assert any(item["reason"] == "CAPTION_OWNED_BY_DIFFERENT_ROLE" and not item["eligible_for_field"] for item in traces["amount"])


def test_tax_percentage_is_a_caption_not_an_observed_tax_amount():
    observations, _ = header_observations_v2([page(block("IVA 19%"))], [decimal("tax", "IVA")])
    assert observations["tax"] == []


def test_undeclared_currency_symbol_never_becomes_verified_monetary_suffix():
    config = decimal("total", "Total due")
    cells, traces = resolved([config], page(block("Total due XYZ 100,00")))
    assert cells["total"]["validity"] == "AMBIGUOUS"
    assert all(item["status"] == "UNCERTAIN" for item in traces["total"])


def test_monetary_suffix_in_positively_wrong_section_is_not_corroborated():
    config = decimal("total", "Total due", section="TOTALS")
    cells, traces = resolved([config], page(block("Buyer", [20, 100, 400, 120]),
        block("Total due 100,00", [20, 140, 400, 160])))
    assert cells["total"]["validity"] == "MISSING"
    assert all(not item["eligible_for_field"] and item["source_section"] == "BUYER" for item in traces["total"])


def test_value_equal_to_role_word_without_own_value_is_not_positively_rejected():
    cells, traces = resolved([field("subject", "Subject")], page(
        block("Subject", [20, 100, 400, 120]), block("Vendor", [20, 130, 400, 150])))
    assert cells["subject"]["validity"] == "AMBIGUOUS"
    assert traces["subject"][0]["observed"] == "Vendor"
    assert traces["subject"][0]["eligible_for_field"] is True
    assert traces["subject"][0]["reason"] == "TARGET_CAPTION_ROLE_UNPROVEN"


def test_another_adjacent_td_does_not_make_role_word_its_owner():
    cells, traces = resolved([field("subject", "Subject")], page(html=
        "<table><tr><td>Subject</td><td>Vendor</td><td>Address</td><td>Example street</td></tr></table>"))
    assert cells["subject"]["validity"] == "AMBIGUOUS"
    assert traces["subject"][0]["observed"] == "Vendor"
    assert traces["subject"][0]["eligible_for_field"] is True


def test_invalid_literal_in_logical_html_section_is_retained_without_geometry():
    config = field("ref", "Reference", visual={"section_label": "Supplier", "value_pattern": r"^(\d+)$"})
    cells, traces = resolved([config], page(html="<table><tr><th colspan='2'>Supplier</th></tr>"
        "<tr><td colspan='2'>Reference: ??</td></tr></table>"))
    assert cells["ref"]["validity"] == "AMBIGUOUS"
    assert traces["ref"][0]["observed"] == "??" and traces["ref"][0]["eligible_for_field"]
    assert traces["ref"][0]["source_section"] == "ISSUER"
    assert traces["ref"][0]["geometry_kind"] == "UNKNOWN"


def test_logical_next_section_prevents_reusing_earlier_section_scope():
    config = field("ref", "Reference", visual={"section_label": "Supplier"})
    cells, traces = resolved([config], page(html="<table><tr><th colspan='2'>Supplier</th></tr>"
        "<tr><th colspan='2'>Buyer</th></tr><tr><td colspan='2'>Reference: ??</td></tr></table>"))
    assert cells["ref"]["validity"] == "MISSING"
    assert traces["ref"][0]["reason"] == "DECLARED_SOURCE_SCOPE_MISMATCH"
    assert traces["ref"][0]["eligible_for_field"] is False


def test_bare_td_order_does_not_prove_section_and_does_not_drop_invalid_literal():
    config = field("ref", "Reference", visual={"section_label": "Supplier"})
    cells, traces = resolved([config], page(html="<table><tr><td>Supplier</td></tr>"
        "<tr><td>Reference: ??</td></tr></table>"))
    assert cells["ref"]["validity"] == "AMBIGUOUS"
    assert traces["ref"][0]["observed"] == "??" and traces["ref"][0]["eligible_for_field"]
    assert traces["ref"][0]["reason"] == "DECLARED_SOURCE_SCOPE_UNPROVEN"
