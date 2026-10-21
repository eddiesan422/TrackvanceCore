"""ID1: missing visual anchors must retain visible uninterpreted table regions."""
import copy
from types import SimpleNamespace

import pytest

from trackvance.document_engine import extract
from trackvance.document_schemas import DocumentDefinition
from trackvance.document_template_builder_rules import box


def source_line(values, y, page):
    words = [{"text": value, "region": [x, y, x + .08, y + .012], "confidence": 95}
             for x, value in values]
    return {"text": " ".join(value for _, value in values), "page": page,
            "region": box(words), "words": words, "confidence": 95, "method": "OCR"}


def table_definition():
    return DocumentDefinition.model_validate({"fields": [{"name": "optional_header", "label": "Optional", "required": False}],
        "tables": [{"name": "detail", "fields": [{"name": "description"}, {"name": "quantity", "logical_type": "DECIMAL"},
            {"name": "amount", "logical_type": "DECIMAL"}], "visual": {"page": 1, "region": [.05, .2, .99, .23],
                "header_labels": ["Description", "Qty", "Amount"], "column_bounds": [0, .45, .7, 1],
                "flowing": True, "continue_pages": True, "multiline_column": 0, "row_tolerance": .027,
                "stop_label": "Footer", "stop_label_aliases": ["Payment summary"]}}]}).model_dump(mode="json")


def two_page_geometry(*, second_header=True):
    # A realistic OCR failure removes the first white-on-dark header while
    # retaining the actual product text and numeric columns underneath it.
    lines = [source_line([(.05, "QA-A first service"), (.5, "2"), (.8, "17.50")], .3, 1),
        source_line([(.05, "QA-B second service"), (.5, "3"), (.8, "21.75")], .4, 1),
        source_line([(.05, "Footer")], .95, 1)]
    if second_header:
        lines.append(source_line([(.05, "Description"), (.5, "Qty"), (.8, "Amount")], .2, 2))
    lines.extend([source_line([(.05, "QA-C third service"), (.5, "4"), (.8, "9.00")], .3, 2),
        source_line([(.05, "Payment summary")], .8, 2),
        source_line([(.05, "Total"), (.8, "48.25")], .85, 2),
        source_line([(.05, "Footer")], .95, 2)])
    return lines


@pytest.mark.parametrize("split_numeric_baseline", [False, True])
def test_missing_first_page_header_cannot_silently_publish_a_partial_later_table(split_numeric_baseline):
    source = two_page_geometry()
    if split_numeric_baseline:
        for line in source[:2]:
            for word in line["words"][1:]:
                word["region"][1] += .02
                word["region"][3] += .02
            line["region"] = box(line["words"])
    before = copy.deepcopy(source)
    payload = extract(source, table_definition())
    assert [row["description"]["value"] for row in payload["tables"]["detail"]] == ["QA-C third service"]
    unparsed = payload["table_unparsed"]["detail"]
    assert any(region["page"] == 1 and region.get("reason") == "TABLE_HEADER_NOT_FOUND"
               and "QA-A first service" in region["text"] and "QA-B second service" in region["text"] for region in unparsed)
    assert not any(region["page"] == 2 for region in unparsed)  # Summary is not a product row.
    assert source == before
    from trackvance.document_service import issues

    for cell in [*payload["fields"].values(), *[cell for row in payload["tables"]["detail"] for cell in row.values()]]:
        cell["reviewed"] = True
    # Confirming only the interpreted later rows cannot silently clear the
    # missing first-page region or authorize publication of an incomplete table.
    assert issues(SimpleNamespace(payload=payload), table_definition()) == ["TABLE_ROWS_UNRECOGNIZED:detail"]


def test_missing_all_headers_keeps_both_pages_visible_instead_of_zero_rows_without_warning():
    payload = extract(two_page_geometry(second_header=False), table_definition())
    assert payload["tables"]["detail"] == []
    unparsed = payload["table_unparsed"]["detail"]
    assert {region["page"] for region in unparsed} == {1, 2}
    assert all(region.get("reason") == "TABLE_HEADER_NOT_FOUND" for region in unparsed)
    assert "QA-C third service" in next(region["text"] for region in unparsed if region["page"] == 2)
    assert all("Payment summary" not in region["text"] and "48.25" not in region["text"] for region in unparsed)


def test_missing_first_header_cannot_drop_body_above_a_repeated_header_on_the_same_page():
    source = two_page_geometry()
    source.extend([source_line([(.05, "Description"), (.5, "Qty"), (.8, "Amount")], .6, 1),
                   source_line([(.05, "QA-D fourth service"), (.5, "5"), (.8, "3.00")], .7, 1)])
    payload = extract(source, table_definition())
    assert [row["description"]["value"] for row in payload["tables"]["detail"]] == ["QA-D fourth service", "QA-C third service"]
    assert any(region["page"] == 1 and region.get("reason") == "TABLE_HEADER_NOT_FOUND"
               and "QA-A first service" in region["text"] and "QA-B second service" in region["text"]
               for region in payload["table_unparsed"]["detail"])
