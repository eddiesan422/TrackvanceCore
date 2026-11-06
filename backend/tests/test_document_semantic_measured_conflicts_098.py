"""Two independently measured captions must retain their monetary occurrences."""
import pytest
import test_document_semantic_corroboration_098 as fixtures
from test_document_semantic_corroboration_v2_098 import assess, mutate

from trackvance.document_semantic_general import compile_profile

PATH = "headers.0.total"
CONTRADICTION = "DOCUMENTARY_SOURCE_CONTRADICTION:" + PATH


@pytest.fixture(autouse=True)
def enable_source_verification(monkeypatch):
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_AUTO_ENABLED", "true")


def fixture(monkeypatch, fragments, *, after_detail=False):
    def compile_with_declared_currencies(profile, **_options):
        next(field for field in profile["fields"] if field["name"] == "total")["currency_symbols"] = ["USD", "EUR"]
        return compile_profile(profile, automatic_verification="SEMANTIC_AUTO_V2")

    with monkeypatch.context() as context:
        context.setattr(fixtures, "compile_profile", compile_with_declared_currencies)
        definition, configuration, events = fixtures.corroboration_fixture()

    def measured(body):
        lines = body["payload"]["lines"]
        lines[:] = [line for line in lines if not line["text"].startswith("Total due:")]
        if after_detail:
            lines.append(fixtures._line("Total due: 10.00", .16))
        top = .36 if after_detail else .16
        for text, left in zip(fragments, [.05, .36, .49, .83]):
            lines.append(fixtures._line(text, top, left=left))

    mutate(events, "NATIVE_SOURCE_PROBE_JSON", measured)
    return assess(definition, configuration, events)


@pytest.mark.parametrize("first,second", [("Grand total", "Amount due"), ("Total a pagar USD", "Total a pagar USD")])
def test_separated_complete_same_role_pairs_retain_conflict_and_evidence(monkeypatch, first, second):
    data, _context, proof = fixture(monkeypatch, [first, "10.00", second, "20.00"])
    assert CONTRADICTION in proof["global_issues"]
    assert PATH not in proof["verified_cells"]
    assert data["payload"]["fields"]["total"]["value"] == "10.00"
    observation = proof["corroboration"][PATH]
    measured = [item for item in observation["association"] if item["basis"] == "PDF_NATIVE_MEASURED_MONETARY_PAIR"]
    assert {item["value"] for item in measured} == {"10.00", "20.00"}
    assert len({item["source_ref"]["id"] for item in measured}) == 1  # same original band
    assert len({item["source_ref"]["occurrence_id"] for item in measured}) == 2
    assert all(item["source_ref"]["label_word_ids"] and item["source_ref"]["value_word_ids"] for item in measured)
    assert not proof["table_coverage"]["items"]  # independent unresolved band still retained


@pytest.mark.parametrize("fragments", [
    ["Grand total", "10.0", "Amount due", "10.00"],
    ["Grand total", "10.00", "Amount due", "??"],
    ["Grand total", "10.00", "Tax", "20.00"],
    ["Grand total USD", "10.00", "Amount due EUR", "20.00"],
    ["Grand total", "10.00 USD", "Amount due", "20.00 EUR"],
])
def test_unproven_or_equivalent_pairs_cannot_create_documentary_conflict(monkeypatch, fragments):
    _data, _context, proof = fixture(monkeypatch, fragments)
    assert CONTRADICTION not in proof["global_issues"]
    assert all(item["basis"] != "PDF_NATIVE_MEASURED_MONETARY_PAIR" for item in proof["corroboration"][PATH]["association"])


def test_one_ordinary_measured_pair_still_corrobates(monkeypatch):
    _data, _context, proof = fixture(monkeypatch, ["Total due", "10.00"])
    assert PATH in proof["verified_cells"]
    assert CONTRADICTION not in proof["global_issues"]


def test_body_owned_compound_caption_cannot_become_header_conflict(monkeypatch):
    _data, _context, proof = fixture(monkeypatch, ["Grand total", "10.00", "Amount due", "20.00"], after_detail=True)
    assert CONTRADICTION not in proof["global_issues"]
    assert not proof["table_coverage"]["items"]
