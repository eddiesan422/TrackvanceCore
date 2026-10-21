"""Label punctuation is separate from values and from column identity."""
import copy

import pytest
from test_document_ocr_visibility_097 import source_line, table_definition

from trackvance.document_engine import extract
from trackvance.document_template_builder_rules import anchor


@pytest.mark.parametrize(("label", "observed"), [("Qty.", "Qty"), ("Qty", "Qty.")])
def test_one_terminal_abbreviation_period_is_optional_for_an_anchor(label, observed):
    assert anchor(source_line([(.1, observed)], .2, 1), label) is not None


@pytest.mark.parametrize(("label", "observed"), [("A.B", "AB"), ("12.5", "125"),
    ("Part-No", "PartNo"), ("Qty..", "Qty"), ("Tax %", "Tax")])
def test_anchor_normalization_does_not_erase_meaningful_internal_or_symbol_content(label, observed):
    assert anchor(source_line([(.1, observed)], .2, 1), label) is None


def test_anchor_abbreviation_recovery_preserves_identifier_punctuation_and_leading_zeros():
    definition = table_definition()
    definition["fields"][0].update(name="identifier", logical_type="STRING", required=True,
        visual={"page": 1, "region": [.1, .1, .9, .2], "label": "Identifier.", "direction": "RIGHT"})
    from trackvance.document_schemas import DocumentDefinition

    definition = DocumentDefinition.model_validate(definition).model_dump(mode="json")
    lines = [source_line([(.1, "Identifier"), (.5, "00.01-02")], .15, 1)]
    payload = extract(lines, definition)
    assert payload["fields"]["identifier"]["value"] == "00.01-02"


def test_table_labels_collapsing_after_abbreviation_normalization_cannot_choose_first_column():
    definition = table_definition()
    definition["tables"][0]["visual"]["header_labels"] = ["Qty", "Qty.", "Amount"]
    source = [source_line([(.05, "Qty"), (.5, "Qty."), (.8, "Amount")], .2, 1),
              source_line([(.05, "2"), (.5, "3"), (.8, "17.50")], .3, 1)]
    payload = extract(source, definition)
    assert payload["tables"]["detail"] == []
    assert payload["table_unparsed"]["detail"]


@pytest.mark.parametrize("has_standalone_amount", [False, True])
def test_similar_headers_use_distinct_nonoverlapping_text_spans(has_standalone_amount):
    definition = table_definition()
    definition["tables"][0]["visual"]["header_labels"] = ["Net amount", "Amount", "Qty"]
    values = [(.05, "Net"), (.15, "amount"), (.8, "Qty")]
    if has_standalone_amount:
        values.append((.5, "Amount"))
    source = [source_line(sorted(values), .2, 1),
              source_line([(.05, "17.50"), (.5, "21.75"), (.8, "3")], .3, 1)]
    before = copy.deepcopy(source)
    payload = extract(source, definition)
    assert source == before
    if has_standalone_amount:
        assert [[cell["value"] for cell in row.values()] for row in payload["tables"]["detail"]] == [["17.50", "21.75", "3"]]
        assert payload["table_unparsed"]["detail"] == []
    else:
        assert payload["tables"]["detail"] == []
        assert payload["table_unparsed"]["detail"]


def test_header_provenance_cannot_cover_unknown_tokens_by_overlapping_value_spans():
    from trackvance.document_engine import validate_value
    from trackvance.document_template_builder_rules import prefix_furniture_words

    definition = table_definition()
    table = definition["tables"][0]
    source = [source_line([(.05, "A"), (.5, "B"), (.8, "C")], .05, 2)]
    assert not prefix_furniture_words(source, 2, 0, .2, table["visual"], table["fields"], ["A B", "B C"], validate_value)


def test_scalar_safety_uses_translation_of_the_observed_continuation_header():
    from trackvance.document_schemas import DocumentDefinition

    definition = table_definition()
    definition["fields"] = [{"name": "known", "logical_type": "STRING", "label": "Proof", "pattern": "(.+)"}]
    definition["tables"][0]["fields"] = [{"name": "description"}, {"name": "quantity", "logical_type": "DECIMAL"},
        {"name": "lot"}, {"name": "amount", "logical_type": "DECIMAL"}]
    definition["tables"][0]["visual"].update(region=[.15, .2, .99, .23],
        header_labels=["Description", "Qty", "Lot", "Amount"], column_bounds=[.1, .4, .6, .8, .99])
    definition = DocumentDefinition.model_validate(definition).model_dump(mode="json")
    source = [source_line([(.05, "Proof"), (.3, "A 5 B C")], .1, 1),
        source_line([(.15, "Description"), (.45, "Qty"), (.65, "Lot"), (.85, "Amount")], .2, 1),
        source_line([(.15, "QA-A"), (.45, "2"), (.65, "L1"), (.85, "17.50")], .3, 1),
        source_line([(.15, "Footer")], .95, 1),
        source_line([(.15, "A"), (.31, "5"), (.65, "B"), (.82, "C")], .05, 2),
        source_line([(.05, "Description"), (.35, "Qty"), (.55, "Lot"), (.75, "Amount")], .2, 2),
        source_line([(.05, "QA-B"), (.35, "3"), (.55, "L2"), (.75, "21.75")], .3, 2)]
    payload = extract(source, definition)
    assert len(payload["tables"]["detail"]) == 2
    assert any("A 5 B C" in region["text"] for region in payload["table_unparsed"]["detail"])


def test_missing_optional_legacy_string_type_remains_supported_with_a_visual_table():
    definition = table_definition()
    definition["fields"][0].pop("logical_type")
    source = [source_line([(.05, "Description"), (.5, "Qty"), (.8, "Amount")], .2, 1),
              source_line([(.05, "QA-A"), (.5, "2"), (.8, "17.50")], .3, 1)]
    payload = extract(source, definition)
    assert payload["fields"]["optional_header"]["validity"] == "VALID"
    assert payload["fields"]["optional_header"]["value"] is None
    assert len(payload["tables"]["detail"]) == 1
