"""Synthetic source counters; no fixture claims independent OCR quality."""
import pytest

from trackvance.document_printed_pagination import assess_documentary_pagination


def fact(text, page=1, *, mechanism="PDF_NATIVE", measured=True):
    return {"text": text, "page": page, "region": [.1, .94, .8, .96] if measured else None,
        "ref": {"id": f"{page}-{text}", "mechanism": mechanism, "stage": "SOURCE",
            "artifact_sha256": "a" * 64, "geometry_kind": "MEASURED_SOURCE" if measured else "UNKNOWN"}}


def report(facts, physical=(1,), processed=None):
    return assess_documentary_pagination(facts, physical_pages=list(physical),
        processed_pages=list(physical if processed is None else processed))


def test_measured_footer_plus_explicit_invoice_continuation_proves_incomplete_source():
    result = report([fact("Example | Página 1 de 2"), fact("Continúa en la hoja 2 de esta factura.")])
    assert result["status"] == "INCOMPLETE"
    assert result["missing_execution_pages"] == []
    assert result["physical_pages"] == result["processed_pages"] == [1]
    assert result["sequences"][0]["scope"] == "DOCUMENT"
    assert result["sequences"][0]["missing_logical_numbers"] == [2]
    assert result["reasons"] == ["DOCUMENT_SOURCE_INCOMPLETE"]


def test_two_processed_physical_pages_do_not_hide_missing_printed_middle_page():
    result = report([fact("Document | Page 1 of 3", 1), fact("Document | Page 3 of 3", 2)], physical=(1, 2))
    assert result["missing_execution_pages"] == []
    assert result["sequences"][0]["missing_logical_numbers"] == [2]
    assert result["status"] == "INCOMPLETE"


def test_section_counter_has_its_own_scope_beside_complete_global_pagination():
    facts = [fact(f"Document | Page {page} of 3", page) for page in (1, 2, 3)]
    facts.extend([fact("Annex A | Page 1 of 2", 2), fact("Annex A | Page 2 of 2", 3)])
    result = report(facts, physical=(1, 2, 3))
    assert result["status"] == "CONSISTENT" and result["blocking"] is False
    assert result["documentary_complete"] is True
    assert {(sequence["scope"], sequence["expected_total"]) for sequence in result["sequences"]} == {("DOCUMENT", 3), ("SECTION", 2)}


def test_valid_partly_observed_section_does_not_manufacture_global_missing_pages():
    result = report([fact("Annex A | Page 1 of 2", 2)], physical=(1, 2, 3))
    assert result["blocking"] is False
    assert result["documentary_complete"] is None
    assert result["sequences"][0]["status"] == "PARTIALLY_OBSERVED"
    assert "DOCUMENT_SOURCE_INCOMPLETE" not in result["reasons"]


def test_unmeasured_unscoped_counter_is_unverified_not_global():
    result = report([fact("Page 1 of 2", measured=False)])
    assert result["status"] == "UNVERIFIED" and result["blocking"] is True
    assert result["observations"][0]["scope"] == "UNKNOWN"
    assert "DOCUMENT_SOURCE_INCOMPLETE" not in result["reasons"]


def test_qwen_only_pagination_and_continuation_cannot_prove_source_scope():
    result = report([fact("Document Page 1 of 2", mechanism="QWEN"),
        fact("Continued on page 2 of this invoice", mechanism="QWEN")])
    assert result["status"] == "UNVERIFIED"
    assert result["observations"][0]["independent"] is False
    assert result["reasons"] == ["PRINTED_PAGINATION_UNCORROBORATED"]


def test_model_continuation_cannot_lend_authority_to_independent_unscoped_footer():
    result = report([fact("Page 1 of 2"), fact("Continued on page 2 of this invoice", mechanism="QWEN")])
    assert result["observations"][0]["scope"] == "UNKNOWN"
    assert result["reasons"] == ["PRINTED_PAGINATION_UNCORROBORATED", "PRINTED_SCOPE_UNPROVEN"]


def test_dates_fractions_item_counts_and_citations_do_not_become_global_counters():
    result = report([fact("1/2/2026; 1/2; item 1 of 2"), fact("See Annex A page 1 of 2")])
    assert result["status"] == "NOT_OBSERVED"
    assert result["documentary_complete"] is None
    assert result["ignored_mentions"][0]["reason"] == "PAGINATION_CITATION_NOT_COUNTER"


def test_no_counter_observed_is_not_documentary_completeness():
    result = report([fact("Document title")])
    assert result["status"] == "NOT_OBSERVED" and result["documentary_complete"] is None


def test_counter_with_number_larger_than_total_is_source_conflict():
    result = report([fact("Invoice Page 3 of 2")])
    assert result["status"] == "CONFLICT"
    assert result["reasons"] == ["PRINTED_PAGINATION_CONFLICT"]


def test_execution_gap_is_kept_distinct_from_documentary_gap():
    result = report([fact("Document Page 1 of 2")], physical=(1, 2), processed=(1,))
    assert result["missing_execution_pages"] == [2]
    assert "PAGE_EXECUTION_INCOMPLETE" in result["reasons"]
    assert "DOCUMENT_SOURCE_INCOMPLETE" not in result["reasons"]


def test_unbounded_counter_retains_literal_without_allocating_claimed_pages():
    result = report([fact("Document Page 1 of 999999")])
    assert result["observations"][0]["literal"] == "Page 1 of 999999"
    assert result["status"] == "UNVERIFIED"
    assert result["sequences"] == []


def test_same_counter_from_two_independent_sources_is_not_a_duplicate_page():
    result = report([fact("Invoice Page 1 of 1"), fact("Invoice Page 1 of 1", mechanism="PADDLE")])
    assert result["status"] == "CONSISTENT"
    assert result["documentary_complete"] is True


def test_conflicting_totals_in_same_explicit_document_scope_block():
    result = report([fact("Invoice Page 1 of 2"), fact("Invoice Page 2 of 3", 2)], physical=(1, 2))
    assert result["status"] == "CONFLICT"


def test_dependent_paddle_counter_cannot_be_treated_as_independent():
    value = fact("Invoice Page 1 of 2", mechanism="PADDLE")
    value["dependencies"] = ["QWEN"]
    result = report([value])
    assert result["status"] == "UNVERIFIED"
    assert result["reasons"] == ["PRINTED_PAGINATION_UNCORROBORATED"]


def test_continuation_scope_target_beyond_proved_global_total_is_conflict():
    continuation = fact("Continúa en la hoja 2 de esta factura.")
    result = report([fact("Document | Page 1 of 1"), continuation])
    assert result["status"] == "CONFLICT" and result["blocking"]
    assert result["reasons"] == ["PRINTED_PAGINATION_CONFLICT"]
    assert result["missing_execution_pages"] == []
    assert result["continuations"][0]["target_printed_page"] == 2
    assert result["continuations"][0]["source_refs"] == [continuation["ref"]]


def test_continuation_scope_without_global_counter_is_uncertainty_without_invented_total():
    continuation = fact("This statement continues on page 2.", mechanism="PADDLE")
    result = report([continuation])
    assert result["status"] == "UNVERIFIED" and result["blocking"]
    assert result["reasons"] == ["PRINTED_SEQUENCE_UNVERIFIED"]
    assert result["observations"] == result["sequences"] == []
    assert result["documentary_complete"] is None
    assert result["continuations"][0]["target_printed_page"] == 2
    assert result["continuations"][0]["source_refs"] == [continuation["ref"]]


@pytest.mark.parametrize("mention", [
    "No continúa en la hoja 2 de esta factura.",
    "Si esta factura continúa en la hoja 2, consulte el anexo.",
    "Véase la nota: «Continúa en la hoja 2 de esta factura».",
])
def test_continuation_scope_nonaffirmative_text_cannot_prove_document_scope(mention):
    result = report([fact("Page 1 of 1"), fact(mention)])
    assert result["observations"][0]["scope"] == "UNKNOWN"
    assert result["reasons"] == ["PRINTED_SCOPE_UNPROVEN"]
    assert result["continuations"] == []


def test_continuation_scope_never_compares_document_destination_to_annex_total():
    result = report([fact("Annex A | Page 1 of 1"), fact("Continued on page 2 of this invoice.")])
    assert result["status"] == "UNVERIFIED"
    assert result["sequences"][0]["scope"] == "SECTION"
    assert result["sequences"][0]["status"] == "CONSISTENT"
    assert result["reasons"] == ["PRINTED_SEQUENCE_UNVERIFIED"]


def test_document_caption_prefix_requires_complete_owner_not_prose_suffix():
    result = report([fact("Copia de otro documento Página 1 de 1")])
    assert result["observations"][0]["scope"] == "UNKNOWN"
    assert result["reasons"] == ["PRINTED_SCOPE_UNPROVEN"]
    assert result["documentary_complete"] is None


@pytest.mark.parametrize("caption", ["Anexo | Página 1 de 2", "Annex-A | Page 1 of 2"])
def test_section_footer_cannot_supply_missing_global_counter(caption):
    result = report([fact(caption), fact("Continúa en la hoja 2 de esta factura."),
        fact("Documento | Página 2 de 2", 2)], physical=(1, 2))
    assert result["observations"][0]["scope"] == "SECTION"
    assert result["documentary_complete"] is None and result["blocking"]
    assert result["reasons"] == ["PRINTED_SEQUENCE_UNVERIFIED"]


def test_continuation_does_not_promote_competing_footer_over_explicit_document_counter():
    explicit = fact("Document | Page 1 of 2")
    other = fact("Copy | Page 2 of 2")
    second = fact("Document | Page 2 of 2", 2)
    result = report([other, explicit, fact("Continued on page 2 of this invoice."), second], physical=(1, 2))
    assert result["observations"][0]["scope"] == "UNKNOWN"
    assert result["observations"][0]["source_refs"] == [other["ref"]]
    assert result["status"] == "UNVERIFIED" and result["blocking"]
    assert result["reasons"] == ["PRINTED_SCOPE_UNPROVEN"]
    assert result["continuations"][0]["counter_refs"] == [explicit["ref"], second["ref"]]


def test_continuation_cannot_choose_between_distinct_unscoped_footers():
    result = report([fact("Copy | Page 2 of 2"), fact("Example | Page 1 of 2"),
        fact("Continued on page 2 of this invoice.")], physical=(1, 2))
    assert [item["scope"] for item in result["observations"]] == ["UNKNOWN", "UNKNOWN"]
    assert result["status"] == "UNVERIFIED" and result["blocking"]
    assert result["reasons"] == ["PRINTED_SCOPE_UNPROVEN", "PRINTED_SEQUENCE_UNVERIFIED"]


@pytest.mark.parametrize(("adjacent", "separated", "expected"), [
    ("Internal working copy", True, "INCOMPLETE"),
    ("Internal working copy", False, "UNVERIFIED"),
    ("Annex A |", True, "UNVERIFIED"),
    ("See terms", True, "UNVERIFIED"),
])
def test_measured_footer_fragments_preserve_separation_and_declared_context(adjacent, separated, expected):
    label = "Q17 | Page 1 of 2"
    footer = fact(f"{adjacent} {label}")
    footer["geometry_kind"] = "MEASURED_SOURCE"
    if separated:
        footer["measured_segments"] = [{"text": adjacent, "region": [.1, .94, .3, .96]},
            {"text": label, "region": [.6, .94, .8, .96]}]
    continuation = fact("Continued on page 2 of this statement.")
    result = report([footer, continuation])
    assert result["status"] == expected and result["blocking"]
    if expected == "INCOMPLETE":
        assert result["observations"][0]["source_refs"] == [footer["ref"], continuation["ref"]]
        assert result["reasons"] == ["DOCUMENT_SOURCE_INCOMPLETE"]
    else:
        assert "DOCUMENT_SOURCE_INCOMPLETE" not in result["reasons"]
