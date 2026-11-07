"""A native adapter must not discard a preceding reader's contradiction."""
import copy

from trackvance.document_engine import extract
from trackvance.document_lossless import map_representation
from trackvance.document_representation import VERSION


def fixture(values):
    definition = {"fields": [{"name":"reference", "label":"Reference", "pattern":r"(.+)",
                              "logical_type":"STRING", "required":True}], "tables":[]}
    lines = [{"text":"Reference: "+value, "page":1, "region":[.1,.1+i*.1,.8,.15+i*.1],
              "method":"DIGITAL", "confidence":None} for i,value in enumerate(values)]
    ir = {"version":VERSION, "pages":[{"id":"synthetic-native-page", "page_one_based":1,
        "stage":"DIGITAL_NATIVE", "native":{"lines":lines}, "blocks":[], "tables":[]}]}
    return definition,lines,ir


def test_native_remap_preserves_reader_ambiguity_and_all_alternatives():
    definition,lines,ir = fixture(["AB-071", "AB-079"])
    original = copy.deepcopy(ir)
    assert extract(lines,definition)["fields"]["reference"]["validity"] == "AMBIGUOUS"
    cell = map_representation(ir,definition)["fields"]["reference"]
    assert cell["validity"] == "AMBIGUOUS" and cell["value"] is None
    assert set(cell["candidate_alternatives"]) == {"AB-071", "AB-079"}
    assert ir == original


def test_native_remap_keeps_an_identical_repeated_value_resolved():
    definition,_,ir = fixture(["AB-071", "AB-071"])
    cell = map_representation(ir,definition)["fields"]["reference"]
    assert cell["validity"] == "VALID" and cell["value"] == "AB-071"
