"""ID1: declared wrapped captions retain geometry and do not become products."""
import copy

import pytest

from trackvance.document_engine import extract
from trackvance.document_schemas import DocumentDefinition
from trackvance.document_template_builder_rules import box


def line(values, y, page=1, method="OCR"):
    tokens = [{"text": text, "region": [x, y, x + .065, y + .009], "confidence": 95}
              for x, text in values]
    return {"text": " ".join(text for _, text in values), "page": page, "method": method,
            "words": tokens, "region": box(tokens), "confidence": 95}


def definition():
    return DocumentDefinition.model_validate({"fields": [{"name": "optional", "required": False}], "tables": [{"name": "detail", "fields": [
        {"name": "description", "label": "Description"},
        {"name": "quantity", "label": "Qty units", "logical_type": "DECIMAL"},
        {"name": "amount", "label": "Amount %", "logical_type": "DECIMAL"}], "visual": {
            "page": 1, "region": [.05, .2, .99, .245],
            "header_labels": ["Description", "Qty", "Amount"], "column_bounds": [0, .45, .7, 1],
            "flowing": True, "continue_pages": True, "multiline_column": 0,
            "row_tolerance": .027, "stop_label": "Footer"}}]}).model_dump(mode="json")


def source(*, method="OCR", dense=False, second_page=False):
    lines = [line([(.05, "Description"), (.5, "Qty"), (.8, "Amount")], .2, method=method),
             line([(.5, "units"), (.8, "%")], .218, method=method),
             line([(.05, "QA-A service"), (.5, "2"), (.8, "17.50")], .238 if dense else .3, method=method),
             line([(.05, "QA-B units % service"), (.5, "3"), (.8, "21.75")], .4, method=method),
             line([(.05, "Footer")], .9, method=method)]
    if second_page:
        lines.extend([line([(.05, "Description"), (.5, "Qty"), (.8, "Amount")], .12, 2, method),
                      line([(.5, "units"), (.8, "%")], .138, 2, method),
                      line([(.05, "QA-C service"), (.5, "4"), (.8, "9.00")], .2, 2, method),
                      line([(.05, "Footer")], .9, 2, method)])
    return lines


@pytest.mark.parametrize("method", ["DIGITAL", "OCR"])
@pytest.mark.parametrize("dense,second_page", [(False, False), (True, False), (False, True)])
def test_declared_wrapped_captions_preserve_products_that_begin_after_the_caption(method, dense, second_page):
    data = source(method=method, dense=dense, second_page=second_page)
    before = copy.deepcopy(data)
    result = extract(data, definition())
    expected = [["QA-A service", "2", "17.50"], ["QA-B units % service", "3", "21.75"]]
    if second_page:
        expected.append(["QA-C service", "4", "9.00"])
    assert [[cell["value"] for cell in row.values()] for row in result["tables"]["detail"]] == expected
    # Even the dense product starts below the complete caption glyphs, with
    # a visible gap. Interleaved/overlapping products are separate scenarios.
    assert result["table_unparsed"]["detail"] == []
    assert data == before


def test_unknown_caption_token_remains_visible_instead_of_being_inferred_from_units_or_percent():
    data = source()
    data[1] = line([(.5, "pieces"), (.8, "%")], .218)
    result = extract(data, definition())
    assert any("pieces" in region["text"] for region in result["table_unparsed"]["detail"])


def test_literal_caption_punctuation_cannot_be_inferred_when_absent_from_original():
    config = definition()
    config["tables"][0]["fields"][1]["label"] = "Qty / units"
    result = extract(source(), config)
    assert any("units" in region["text"] for region in result["table_unparsed"]["detail"])


def test_caption_matching_cannot_consume_a_valid_numeric_product_scalar():
    config = definition()
    config["tables"][0]["fields"][1]["label"] = "Qty 2"
    data = source()
    data[1] = line([(.5, "2"), (.8, "%")], .218)
    result = extract(data, config)
    assert any("2" in region["text"] for region in result["table_unparsed"]["detail"])


def test_caption_words_outside_the_selected_header_height_are_still_subject_to_review():
    data = source()
    data[1] = line([(.5, "units"), (.8, "%")], .27)
    result = extract(data, definition())
    assert any("units" in region["text"] for region in result["table_unparsed"]["detail"])


def test_caption_without_any_observed_anchor_cannot_hide_a_product_like_region():
    data = source()[1:]
    result = extract(data, definition())
    assert result["tables"]["detail"] == []
    assert any("QA-A service" in region["text"] for region in result["table_unparsed"]["detail"])


def test_first_product_before_last_caption_and_split_numeric_baseline_is_not_truncated():
    data = [line([(.05, "Description"), (.5, "Qty"), (.8, "Amount")], .2),
            line([(.5, "units")], .224), line([(.8, "%")], .242),
            line([(.05, "UND Hours % service")], .218),
            line([(.5, "2"), (.8, "17.50")], .232), line([(.05, "Footer")], .9)]
    config = definition()
    config["tables"][0]["visual"]["region"][3] = .27  # Selection also contains the complete first product.
    result = extract(data, config)
    regions = result["table_unparsed"]["detail"]
    assert regions  # Product and literal caption interleave: require review.
    text = " ".join(region["text"] for region in regions)
    assert all(token in text for token in ("UND Hours % service", "2", "17.50"))


def test_separate_numeric_baselines_remain_visible_when_row_attribution_is_ambiguous():
    data = [line([(.05, "Description"), (.5, "Qty"), (.8, "Amount")], .2),
            line([(.5, "units")], .224), line([(.8, "%")], .242),
            line([(.05, "UND Hours % service"), (.5, "2")], .232),
            line([(.8, "17.50")], .252), line([(.05, "Footer")], .9)]
    config = definition()
    config["tables"][0]["visual"]["region"][3] = .27
    result = extract(data, config)
    regions = result["table_unparsed"]["detail"]
    assert regions  # Existing row-boundary policy cannot silently lose the product.
    text = " ".join(region["text"] for region in regions)
    assert all(token in text for token in ("UND Hours % service", "2", "17.50"))


def test_wrapped_caption_uses_the_translation_of_the_observed_header():
    data = source()
    for entry in data:
        for word in entry["words"]:
            word["region"][0] += .04
            word["region"][2] += .04
        entry["region"] = box(entry["words"])
    result = extract(data, definition())
    assert len(result["tables"]["detail"]) == 2
    assert result["table_unparsed"]["detail"] == []


def test_all_string_caption_and_product_ambiguity_remains_visible():
    config = definition()
    for field in config["tables"][0]["fields"]:
        field["logical_type"] = "STRING"
    result = extract(source(), config)
    assert any("units" in region["text"] for region in result["table_unparsed"]["detail"])


def test_caption_words_have_vertical_reading_order_when_ocr_x_order_interleaves_them():
    data = source()
    data[1] = line([(.49, "units"), (.79, "%")], .211)
    result = extract(data, definition())
    assert len(result["tables"]["detail"]) == 2
    assert result["table_unparsed"]["detail"] == []


def test_wrapped_caption_preserves_a_literal_slash_and_terminal_abbreviation_dot():
    config = definition()
    config["tables"][0]["fields"][1]["label"] = "Qty / UND."
    data = source()
    data[0] = line([(.05, "Description"), (.5, "Qty"), (.58, "/"), (.8, "Amount")], .2)
    data[1] = line([(.5, "UND."), (.8, "%")], .218)
    result = extract(data, config)
    assert len(result["tables"]["detail"]) == 2
    assert result["table_unparsed"]["detail"] == []


@pytest.mark.parametrize("split_baseline", [False, True])
def test_literal_caption_cannot_consume_a_real_percent_description_with_valid_product_scalars(split_baseline):
    config = definition()
    config["tables"][0]["fields"][0]["label"] = "Description %"
    data = [line([(.05, "Description"), (.5, "Qty"), (.8, "Amount")], .2),
            line([(.05, "%")], .22),
            line([(.5, "2"), (.8, "17.50")], .24 if split_baseline else .22),
            line([(.05, "Footer")], .9)]
    config["tables"][0]["visual"]["region"][3] = .27
    result = extract(data, config)
    assert [[cell["value"] for cell in row.values()] for row in result["tables"]["detail"]] == [["%", "2", "17.50"]]


def test_large_example_height_cannot_consume_a_distant_uninterpreted_product_fragment():
    config = definition()
    config["tables"][0]["visual"]["region"][3] = .5
    data = [line([(.05, "Description"), (.5, "Qty"), (.8, "Amount")], .2),
            line([(.5, "units")], .35), line([(.05, "Footer")], .9)]
    result = extract(data, config)
    assert result["tables"]["detail"] == []
    assert any("units" in region["text"] for region in result["table_unparsed"]["detail"])


def test_large_example_height_keeps_a_real_near_caption_and_all_later_products():
    config = definition()
    config["tables"][0]["visual"]["region"][3] = .5
    result = extract(source(), config)
    assert [[cell["value"] for cell in row.values()] for row in result["tables"]["detail"]] == [
        ["QA-A service", "2", "17.50"], ["QA-B units % service", "3", "21.75"]]
    assert result["table_unparsed"]["detail"] == []


@pytest.mark.parametrize("geometry", ["description_before_caption", "scalar_between_captions"])
def test_interleaved_body_cues_keep_the_whole_ambiguous_region_visible(geometry):
    data = [line([(.05, "Description"), (.5, "Qty"), (.8, "Amount")], .2),
            line([(.5, "units")], .211), line([(.8, "%")], .218)]
    if geometry == "description_before_caption":
        data.extend([line([(.05, "SKU-X service")], .212),
                     line([(.5, "2"), (.8, "17.50")], .235)])
    else:
        data.append(line([(.05, "SKU-X service"), (.5, "2"), (.8, "17.50")], .216))
    data.append(line([(.05, "Footer")], .9))
    result = extract(data, definition())
    rows = result["tables"]["detail"]
    regions = result["table_unparsed"]["detail"]
    assert regions or any(cell["validity"] != "VALID" for row in rows for cell in row.values())
    represented = " ".join([*(region["text"] for region in regions),
                            *(str(cell["observed"]) for row in rows for cell in row.values())]).split()
    assert all(token in represented for token in ("units", "%", "SKU-X", "service", "2", "17.50"))


def test_nonmultiline_string_code_identifies_an_ambiguous_numeric_caption_fragment():
    config = DocumentDefinition.model_validate({"fields": [{"name": "optional", "required": False}],
        "tables": [{"name": "detail", "fields": [{"name": "code", "label": "Code"},
            {"name": "description", "label": "Description"},
            {"name": "quantity", "label": "Qty units", "logical_type": "DECIMAL"}],
            "visual": {"page": 1, "region": [.05, .2, .99, .245],
                "header_labels": ["Code", "Description", "Qty"], "column_bounds": [0, .3, .7, 1],
                "flowing": True, "continue_pages": True, "multiline_column": 1,
                "row_tolerance": .027, "stop_label": "Footer"}}]}).model_dump(mode="json")
    data = [line([(.05, "Code"), (.35, "Description"), (.8, "Qty")], .2),
            line([(.05, "SKU-X"), (.8, "units")], .218),
            line([(.35, "Service"), (.8, "2")], .3), line([(.05, "Footer")], .9)]
    result = extract(data, config)
    represented = " ".join([*(r["text"] for r in result["table_unparsed"]["detail"]),
        *(str(cell["observed"]) for row in result["tables"]["detail"] for cell in row.values())]).split()
    assert all(token in represented for token in ("SKU-X", "units", "Service", "2"))


def test_tight_row_tolerance_cannot_remove_the_protection_of_an_earlier_local_description():
    config = definition()
    config["tables"][0]["visual"]["row_tolerance"] = .002
    data = [line([(.05, "Description"), (.5, "Qty"), (.8, "Amount")], .2),
            line([(.05, "EARLY service")], .212), line([(.5, "units"), (.8, "%")], .218),
            line([(.5, "2"), (.8, "17.50")], .25), line([(.05, "Footer")], .9)]
    result = extract(data, config)
    represented = " ".join([*(r["text"] for r in result["table_unparsed"]["detail"]),
        *(str(cell["observed"]) for row in result["tables"]["detail"] for cell in row.values())]).split()
    assert all(token in represented for token in ("EARLY", "service", "units", "%", "2", "17.50"))


def test_string_identifier_with_leading_zeros_cannot_be_attributed_to_a_caption():
    config = DocumentDefinition.model_validate({"fields": [{"name": "optional", "required": False}],
        "tables": [{"name": "detail", "fields": [{"name": "code", "label": "Code 001"},
            {"name": "description", "label": "Description"},
            {"name": "quantity", "label": "Qty", "logical_type": "DECIMAL"}],
            "visual": {"page": 1, "region": [.05, .2, .99, .245],
                "header_labels": ["Code", "Description", "Qty"], "column_bounds": [0, .3, .7, 1],
                "flowing": True, "continue_pages": True, "multiline_column": 1,
                "row_tolerance": .027, "stop_label": "Footer"}}]}).model_dump(mode="json")
    data = [line([(.05, "Code"), (.35, "Description"), (.8, "Qty")], .2),
            line([(.05, "001")], .218), line([(.35, "Service"), (.8, "2")], .3),
            line([(.05, "Footer")], .9)]
    result = extract(data, config)
    represented = " ".join([*(r["text"] for r in result["table_unparsed"]["detail"]),
        *(str(cell["observed"]) for row in result["tables"]["detail"] for cell in row.values())]).split()
    assert all(token in represented for token in ("001", "Service", "2"))
