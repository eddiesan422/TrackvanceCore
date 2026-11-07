"""A general Spanish page-counter reproduction, separate from the consumed reserve."""
from types import SimpleNamespace

import pytest
import test_document_semantic_corroboration_v2_098 as fixtures

from trackvance.document_semantic_evidence import semantic_review_dto


@pytest.fixture(autouse=True)
def explicit_synthetic_activation(monkeypatch):
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "true")
    monkeypatch.setenv("TRACKVANCE_SEMANTIC_AUTO_ENABLED", "true")


def assessed(monkeypatch,text,top=.94):
    definition,configuration,events=fixtures._detail_ownership_fixture(monkeypatch,"native",
        proposed=[["Steel clips","17.25"]],source_rows=[["Steel clips","17.25"]],scalar_separate=True)
    fixtures.mutate(events,"NATIVE_SOURCE_PROBE_JSON",lambda body:body["payload"]["lines"].append(
        fixtures.fixtures._line(text,top)))
    data,_,proof=fixtures.assess(definition,configuration,events)
    dto=semantic_review_dto(SimpleNamespace(payload=data["payload"],engine=data["engine"],reviewed=False),definition)
    return proof,dto


def test_explicit_spanish_document_counter_does_not_become_an_unparsed_item(monkeypatch):
    proof,dto=assessed(monkeypatch,"Informe hoja 1 de 1")
    assert proof["printed_pagination"]["status"]=="CONSISTENT"
    assert proof["table_coverage"]["items"]
    assert dto["automatic_approval"]


@pytest.mark.parametrize("text,top",[
    ("Informe hoja 1 de 2",.94),
    ("Ver informe hoja 1 de 1",.94),
    ("Informe hoja 1 de 1",.72),
    ("Anexo hoja 1 de 1",.94),
])
def test_incomplete_referenced_or_body_counter_cannot_erase_unparsed_source(monkeypatch,text,top):
    proof,dto=assessed(monkeypatch,text,top)
    assert not proof["table_coverage"]["items"]
    assert not dto["automatic_approval"]


def test_a_complete_row_including_page_words_remains_a_source_occurrence(monkeypatch):
    definition,configuration,events=fixtures._detail_ownership_fixture(monkeypatch,"native",
        proposed=[["Steel clips","17.25"]],source_rows=[["Steel clips","17.25"],["Informe hoja 1 de 1","31.50"]],
        scalar_separate=True)
    def position(body):
        for line in body["payload"]["lines"]:
            if line["text"].startswith("Informe hoja"):
                line["region"][1],line["region"][3]=.94,.952
                for word in line["words"]:word["region"][1],word["region"][3]=.94,.952
    fixtures.mutate(events,"NATIVE_SOURCE_PROBE_JSON",position)
    _,_,proof=fixtures.assess(definition,configuration,events)
    inventory=proof["coverage_details"]["items"]["source_inventory"][0]
    assert len(inventory["rows"])==2
    assert not proof["table_coverage"]["items"]
