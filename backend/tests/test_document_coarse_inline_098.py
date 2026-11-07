"""Generic declared-caption reproductions, unrelated to corpus reference values."""
import copy

import pytest

from trackvance.document_engine import extract
from trackvance.document_template_builder_rules import field_candidates


def rule(**changes):
    return {"page": 1, "region": [.05, .1, .95, .8], "kind": "LABEL_VALUE",
            "label": "Tax identifier", "direction": "RIGHT", "label_aliases": [], "section_label": "",
            "section_bounds": [.05, .45], "value_pattern": r"([A-Z]{2}-[0-9]{4})", **changes}


def source(text="Tax identifier: AB-7428", **changes):
    return {"text": text, "page": 1, "region": [.1, .2, .4, .23], "method": "OCR", "confidence": None, **changes}


def test_declared_inline_caption_retains_measured_whole_block():
    lines = [source()]
    saved = copy.deepcopy(lines)
    result = field_candidates(lines, rule())
    assert [(raw, evidence["region"]) for raw, evidence in result] == [("AB-7428", lines[0]["region"])]
    assert result[0][1]["resolution"] == "LAYOUT_BLOCK"
    assert lines == saved


def test_bilingual_equals_delimiter_and_repeated_real_values_are_preserved():
    lines = [source("Identificador fiscal = CD-8124"), source("Tax identifier: EF-3651", region=[.1,.3,.4,.33])]
    result = field_candidates(lines, rule(label_aliases=["Identificador fiscal"]))
    assert {raw for raw, _ in result} == {"CD-8124", "EF-3651"}


@pytest.mark.parametrize("line,config", [
    (source("Other tax identifier: AB-7428"), rule()),
    (source("Tax identifier AB-7428"), rule()),
    (source(page=2), rule()),
    (source(region=[.4,.2,.6,.23]), rule()),
    (source("Tax identifier: AB-7428 Account: CD-8124"), rule()),
    (source(), rule(section_label="Issuer")),
])
def test_unproven_caption_section_or_geometry_does_not_produce_candidate(line,config):
    assert field_candidates([line],config) == []


def test_existing_measured_words_still_use_real_value_box():
    words = [{"text":"Tax", "region":[.1,.2,.13,.23]},
             {"text":"identifier:","region":[.14,.2,.25,.23]},
             {"text":"AB-7428","region":[.27,.2,.4,.23]}]
    result = field_candidates([source(words=words)],rule())
    assert result[0][0] == "AB-7428" and result[0][1]["region"] == words[-1]["region"]


def test_distinct_inline_values_remain_ambiguous_in_native_selection():
    definition = {"fields": [{"name":"identifier", "label":"Tax identifier", "logical_type":"STRING",
                              "required":True, "visual":rule()}], "tables":[]}
    lines = [source(), source("Tax identifier: CD-8124", region=[.1,.3,.4,.33])]
    cell = extract(lines, definition)["fields"]["identifier"]
    assert cell["validity"] == "AMBIGUOUS"
    assert set(cell["alternatives"]) == {"AB-7428", "CD-8124"}
