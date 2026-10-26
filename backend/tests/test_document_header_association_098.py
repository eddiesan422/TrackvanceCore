"""Declared header relations, never first-value or first-column guessing."""
import copy
import importlib.util
from pathlib import Path

from trackvance.document_lossless import map_representation
from trackvance.document_representation import VERSION, parse_table_html


def field(name, label, **extra):
    return {"name": name, "label": label, "logical_type": "STRING", "required": True, **extra}


def block(text, box=None, *, identity=None):
    return {"id": identity or text, "type": "text", "text": text, "geometry": box,
        "geometry_kind": "MEASURED_BLOCK" if box else "UNKNOWN"}


def page(*blocks, html=None, number=1):
    tables = parse_table_html(html, identity=f"page-{number}") if html else []
    return {"id": f"page-{number}", "page_one_based": number, "stage": "PADDLE_STRUCTURED",
        "geometry": {"width": 1000, "height": 1000, "unit": "PIXEL"},
        "blocks": list(blocks), "tables": tables}


def mapped(fields, *pages):
    return map_representation({"version": VERSION, "pages": list(pages)}, {"fields": fields, "tables": []})


def test_declared_alias_normalizes_label_without_normalizing_observation():
    config = field("identifier", "Identification", visual={"label": "Identification", "label_aliases": ["Código"]})
    result = mapped([config], page(block("CÓDIGO:  0007-AB")))
    assert result["fields"]["identifier"]["observed"] == "0007-AB"
    assert result["fields"]["identifier"]["value"] == "0007-AB"


def test_vertical_blocks_use_measured_relation_and_do_not_take_another_column():
    result = mapped([field("date", "Issued")], page(
        block("Issued", [50, 100, 150, 120]), block("wrong", [650, 130, 750, 150]),
        block("2026-02-17", [50, 132, 170, 152])))
    assert result["fields"]["date"]["observed"] == "2026-02-17"
    assert result["fields"]["date"]["geometry_kind"] == "MEASURED_BLOCK"
    assert result["fields"]["date"]["origin"]["region"] == [.05, .132, .17, .152]


def test_several_pairs_in_one_block_are_bounded_by_the_next_declared_label():
    result = mapped([field("order", "Order"), field("reference", "Reference"), field("term", "Term")],
        page(block("Order: 001-ABC   Reference: REF-93\nTerm: Thirty days")))
    assert {key: cell["observed"] for key, cell in result["fields"].items()} == {
        "order": "001-ABC", "reference": "REF-93", "term": "Thirty days"}


def test_four_column_table_keeps_both_label_value_pairs():
    result = mapped([field("a", "Alpha"), field("b", "Beta")], page(html=
        "<table><tr><td>Alpha</td><td>001</td><td>Beta</td><td>002</td></tr></table>"))
    assert result["fields"]["a"]["observed"] == "001"
    assert result["fields"]["b"]["observed"] == "002"


def test_vertical_table_labels_associate_to_same_column_next_row():
    result = mapped([field("a", "Alpha"), field("b", "Beta")], page(html=
        "<table><tr><th>Alpha</th><th>Beta</th></tr><tr><td>001</td><td>002</td></tr></table>"))
    assert result["fields"]["a"]["observed"] == "001"
    assert result["fields"]["b"]["observed"] == "002"
    assert all(cell["geometry_kind"] != "MEASURED_SOURCE" for cell in result["fields"].values())
    assert all(cell["geometry"] is None for cell in result["document_representation"]["pages"][0]["tables"][0]["cells"])


def test_repeated_party_labels_require_declared_section_relation():
    fields = [field("seller", "Tax ID", visual={"section_label": "Seller", "direction": "RIGHT"}),
        field("buyer", "Tax ID", visual={"section_label": "Buyer", "direction": "RIGHT"})]
    source = page(block("Seller", [50, 100, 200, 120]), block("Buyer", [600, 100, 800, 120]),
        block("Tax ID: 001-8", [50, 140, 240, 160]), block("Tax ID: 002-9", [600, 140, 840, 160]))
    result = mapped(fields, source)
    assert result["fields"]["seller"]["observed"] == "001-8"
    assert result["fields"]["buyer"]["observed"] == "002-9"
    ambiguous = mapped([field("tax_id", "Tax ID")], source)["fields"]["tax_id"]
    assert ambiguous["value"] is None and ambiguous["validity"] == "AMBIGUOUS"


def test_declared_section_is_not_satisfied_by_an_unrelated_heading():
    config = field("email", "Contact", visual={"section_label": "Supplier"})
    result = mapped([config], page(block("Buyer", [30, 30, 140, 50]), block("Contact: x@example.test", [30, 70, 200, 90])))
    assert result["fields"]["email"]["validity"] == "MISSING"


def test_unknown_geometry_does_not_create_relation_from_global_block_order():
    result = mapped([field("email", "Email")], page(block("Email"), block("wrong@example.test")))
    assert result["fields"]["email"]["validity"] == "MISSING"


def test_known_source_outside_declared_window_is_not_admitted_as_a_header():
    config = field("ref", "Reference", visual={"region": [.5, 0, 1, .5]})
    result = mapped([config], page(block("Reference: WRONG", [50, 100, 300, 120]),
        block("Reference: RIGHT", [600, 100, 900, 120])))["fields"]["ref"]
    assert result["observed"] == "RIGHT"


def test_unknown_source_geometry_cannot_claim_an_explicit_search_window():
    config = field("ref", "Reference", visual={"region": [.5, 0, 1, .5]})
    assert mapped([config], page(block("Reference: UNKNOWN")))["fields"]["ref"]["validity"] == "MISSING"
    html = "<table><tr><td>Reference</td><td>UNKNOWN</td></tr></table>"
    assert mapped([config], page(html=html))["fields"]["ref"]["validity"] == "MISSING"
    assert mapped([field("ref", "Reference")], page(html=html))["fields"]["ref"]["observed"] == "UNKNOWN"


def test_label_only_punctuation_is_not_an_extra_numeric_observation():
    fields = [field("tax", "Tax 7 %", logical_type="DECIMAL", decimal_separator=","),
        field("exempt", "Exempt (0 %)", logical_type="DECIMAL", decimal_separator=",")]
    result = mapped(fields, page(html="<table><tr><td>Tax 7 %</td><td>25,00</td></tr>"
        "<tr><td>Exempt (0 %)</td><td>0,00</td></tr></table>"))
    assert result["fields"]["tax"]["observed"] == "25,00"
    assert result["fields"]["exempt"]["observed"] == "0,00"
    assert all(cell["validity"] == "VALID" for cell in result["fields"].values())
    assert all(len(cell["candidate_alternatives"]) == 1 for cell in result["fields"].values())
    assert all(item["rule"] == "DECLARED_TABLE_RIGHT" for trace in result["header_associations"].values() for item in trace)


def test_inline_label_punctuation_is_consumed_without_dropping_signed_values():
    fields = [field("tax", "Tax 7 %"), field("exempt", "Exempt (0 %)")]
    result = mapped(fields, page(block("Tax 7 %: -12,00 Exempt (0 %): 0,00")))
    assert result["fields"]["tax"]["observed"] == "-12,00"
    assert result["fields"]["exempt"]["observed"] == "0,00"


def test_same_block_line_relation_does_not_invent_individual_line_geometry():
    result = mapped([field("ref", "Reference")], page(block("Reference\nR-09")))["fields"]["ref"]
    assert result["observed"] == "R-09" and result["geometry_kind"] == "UNKNOWN"
    assert result["origin"]["geometry_kind"] == "UNKNOWN"
    assert result["origin"]["region_semantics"] == "PAGE_LOCATOR_ONLY"


def test_vertical_relation_cannot_skip_an_intervening_declared_label():
    result = mapped([field("a", "Alpha"), field("b", "Beta")], page(
        block("Alpha", [50, 100, 180, 120]), block("Beta", [50, 125, 180, 145]),
        block("B-007", [50, 150, 180, 170])))
    assert result["fields"]["a"]["validity"] == "MISSING"
    assert result["fields"]["b"]["observed"] == "B-007"


def test_table_sections_use_logical_spans_without_fabricating_cell_rectangles():
    fields = [field("seller", "Tax ID", visual={"section_label": "Supplier", "direction": "RIGHT"}),
        field("buyer", "Tax ID", visual={"section_label": "Customer", "direction": "RIGHT"})]
    result = mapped(fields, page(html="<table><tr><th colspan='2'>Supplier</th><th colspan='2'>Customer</th></tr>"
        "<tr><td>Tax ID</td><td>001</td><td>Tax ID</td><td>002</td></tr></table>"))
    assert result["fields"]["seller"]["observed"] == "001"
    assert result["fields"]["buyer"]["observed"] == "002"
    assert result["fields"]["buyer"]["geometry_kind"] == "UNKNOWN"


def test_equally_near_distinct_values_preserve_ambiguity():
    result = mapped([field("a", "Alpha")], page(block("Alpha", [50, 100, 400, 120]),
        block("LEFT", [50, 130, 180, 150]), block("RIGHT", [220, 130, 400, 150])))["fields"]["a"]
    assert result["validity"] == "AMBIGUOUS" and result["value"] is None
    assert {item["observed"] for item in result["candidate_alternatives"]} == {"LEFT", "RIGHT"}


def test_declared_below_pattern_can_select_a_later_valid_block_within_scope():
    config = field("email", "Tax ID", visual={"label": "Tax ID", "direction": "BELOW",
        "section_label": "Supplier", "value_pattern": r"^([^\s@]+@[^\s@]+\.[^\s@]+)$", "tolerance": .08})
    result = mapped([config], page(block("Supplier", [20, 100, 200, 120]),
        block("Tax ID: 0007", [20, 140, 150, 160]), block("Street 90", [20, 170, 150, 190]),
        block("office@example.test", [20, 200, 220, 220]), block("outsider@example.test", [650, 200, 900, 220])))
    assert result["fields"]["email"]["observed"] == "office@example.test"


def test_declared_page_keeps_an_observation_from_another_page_out_of_scope():
    config = field("ref", "Reference", visual={"page": 2})
    result = mapped([config], page(block("Reference: OLD"), number=1), page(block("Reference: NEW"), number=2))
    assert result["fields"]["ref"]["observed"] == "NEW"


def test_same_label_on_separate_pages_retains_contradiction():
    result = mapped([field("ref", "Reference")], page(block("Reference: A"), number=1),
        page(block("Reference: B"), number=2))["fields"]["ref"]
    assert result["validity"] == "AMBIGUOUS" and result["value"] is None
    assert {item["observed"] for item in result["candidate_alternatives"]} == {"A", "B"}


def test_empty_table_value_stays_empty_and_does_not_take_following_pair():
    result = mapped([field("a", "Alpha"), field("b", "Beta")], page(html=
        "<table><tr><td>Alpha</td><td></td><td>Beta</td><td>002</td></tr></table>"))
    assert result["fields"]["a"]["observed"] == ""
    assert result["fields"]["a"]["validity"] == "MISSING"
    assert result["fields"]["b"]["observed"] == "002"


def test_mapping_preserves_input_document_and_trace_refs_both_sides():
    source = page(html="<table><tr><td>Alpha</td><td>001</td></tr></table>")
    before = copy.deepcopy(source)
    result = mapped([field("a", "Alpha")], source)
    assert source == before
    trace = result["header_associations"]["a"]
    assert trace and all(item["label_refs"] and item["value_refs"] for item in trace)
    assert all(item["version"] == "DECLARED_HEADER_ASSOCIATION_V1" for item in trace)


def test_visual_semantic_metadata_keeps_declared_context_and_excludes_samples():
    path = Path(__file__).resolve().parents[2] / "deploy/idp/recognition.py"
    spec = importlib.util.spec_from_file_location("header_semantic_gateway", path)
    gateway = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gateway)
    definition = {"fields": [field("reference", "Reference", section="SUPPLIER", required=True,
        visual={"label": "Ref", "label_aliases": ["Reference no."], "section_label": "Supplier", "direction": "BELOW", "page": 2},
        visual_alternatives=[{"label": "Identifier", "section_label": "Supplier"}], pattern="PRIVATE_SAMPLE_PATTERN", sample="DO_NOT_SEND")],
        "tables": [{"name": "lines", "structured_header_labels": ["Product"],
            "fields": [field("code", "Code")]}]}
    before = copy.deepcopy(definition)
    metadata = gateway.semantic_definition(definition)
    header = metadata["fields"][0]
    assert header["section"] == "SUPPLIER" and header["required"] is True
    assert set(header["aliases"]) == {"Ref", "Reference no.", "Identifier"}
    assert header["source_context"][0] == {"page": 2, "section_label": "Supplier", "direction": "BELOW", "anchor_label": "Ref"}
    assert metadata["tables"][0]["declared_column_labels"] == ["Product"]
    assert metadata["semantic_definition_version"] == "trackvance-visual-semantic-098-v2"
    assert "PRIVATE_SAMPLE" not in str(metadata) and "DO_NOT_SEND" not in str(metadata)
    assert definition == before
