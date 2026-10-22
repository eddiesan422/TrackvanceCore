"""Opt-in literal anchors and observed row starts; independent generic sources."""
import copy
import json

import pytest
from pydantic import ValidationError

from trackvance import document_template_builder_rules as rules
from trackvance.document_engine import extract
from trackvance.document_template_builder_schemas import VisualTableRule


def line(text, x, y, width=.09, height=.007, page=1):
    region = [x, y, x + width, y + height]
    return {"text": text, "region": region, "page": page, "method": "OCR", "confidence": 90,
            "words": [{"text": text, "region": region.copy(), "confidence": 90}]}


def definition(partial=False):
    names = ["description", "quantity", "price", "amount"]
    labels = ["Description", "Qty", "Price", "Amount"]
    fields = [{"name": name, "label": label, "logical_type": "STRING" if index == 0 else "DECIMAL",
               "required": True, "pattern": "(.+)", "decimal_separator": ".",
               "thousands_separator": None, "currency_symbols": []}
              for index, (name, label) in enumerate(zip(names, labels, strict=True))]
    rule = {"page": 1, "region": [.05, .19, .95, .24], "header_labels": labels,
            "column_bounds": [.04, .40, .60, .78, .96], "multiline_column": 0,
            "row_tolerance": .018, "flowing": True, "continue_pages": True,
            "tolerance": .06, "stop_label": "End", "stop_label_aliases": [],
            "row_start": {"column": 0, "separator": "-", "segments": [
                {"alphabet": "ASCII_VISIBLE", "minimum": 2, "maximum": 4},
                {"alphabet": "ASCII_DIGIT", "minimum": 5, "maximum": 5},
                {"alphabet": "ASCII_DIGIT", "minimum": 2, "maximum": 2}],
                "maximum_start_inset": .035}}
    if partial:
        rule["header_observation_policy"] = "SOURCE_REVIEW_REQUIRED"
    return {"fields": [], "tables": [{"name": "detail", "required": True,
            "fields": fields, "visual": rule}], "arithmetic": []}


def sources(partial=False, page=1, missing=False):
    result = [line(label, x, .2, .18 if index == 0 else .09, page=page)
              for index, (label, x) in enumerate(zip(
                  ["Description", "Qxx" if partial else "Qty", "Price", "Amount"],
                  [.05, .45, .63, .82], strict=True))]
    for index, top in enumerate([.3, .55]):
        result += [line(f"ABC-12345-{index + 1:02d}", .05, top, .20, page=page),
                   line(f"Service {index + 1}", .05, top + .015, .20, page=page),
                   line(str(index + 2), .45, top + .03, page=page),
                   line("35.00" if index == 0 else "52.50", .82, top + .03, page=page)]
        if not missing or index != 0:
            result.append(line("17.50", .63, top + .03, page=page))
    return [*result, line("End", .05, .9, page=page)]


def values(payload):
    return [[cell["value"] for cell in row.values()] for row in payload["tables"]["detail"]]


def test_declared_row_start_recovers_only_observed_cells_and_preserves_source():
    source = sources()
    before = copy.deepcopy(source)
    result = extract(source, definition())
    assert values(result) == [["ABC-12345-01 Service 1", "2", "17.50", "35.00"],
                              ["ABC-12345-02 Service 2", "3", "17.50", "52.50"]]
    assert result["table_unparsed"]["detail"] == []
    assert source == before


def test_explicit_missing_cell_has_no_invented_origin_or_confidence():
    result = extract(sources(missing=True), definition())
    assert len(result["tables"]["detail"]) == 2
    assert result["tables"]["detail"][0]["price"] == {
        "value": None, "observed": None, "validity": "MISSING", "origin": None,
        "confidence": None, "reviewed": False}


def test_partial_header_retains_unknown_words_as_immutable_blocking_observation():
    source, declared = sources(partial=True), definition(partial=True)
    before = copy.deepcopy(source)
    result = extract(source, declared)
    assert len(result["tables"]["detail"]) == 2
    regions = result["table_unparsed"]["detail"]
    assert len(regions) == 1 and regions[0]["reason"] == "HEADER_UNVERIFIED"
    observation = regions[0]["header_observation"]
    assert observation["kind"] == "HEADER_UNVERIFIED"
    assert "Qxx" in [item["text"] for item in observation["source_words"]]
    assert rules.valid_header_observation(regions[0], source, declared, "detail")
    assert source == before


@pytest.mark.parametrize("mutation", ["word", "index", "hash", "table", "bounds", "reason"])
def test_observation_protocol_rejects_forged_source_or_scope(mutation):
    source, declared = sources(partial=True), definition(partial=True)
    region = copy.deepcopy(extract(source, declared)["table_unparsed"]["detail"][0])
    observed = region["header_observation"]
    if mutation == "word":
        observed["source_words"][0]["text"] = "replacement"
    elif mutation == "index":
        observed["source_words"][0]["word_index"] = True
    elif mutation == "hash":
        observed["source_lines_hash"] = "0" * 64
    elif mutation == "table":
        observed["table"] = "other"
    elif mutation == "bounds":
        observed["column_bounds"][0] = .001
    else:
        region["reason"] = "AMBIGUOUS_ROW_BOUNDARY"
    assert not rules.valid_header_observation(region, source, declared, "detail")


@pytest.mark.parametrize("unknown", ["001", "%", "BODY-SKU"])
def test_unknown_partial_header_material_stays_visible(unknown):
    source = [*sources(partial=True), line(unknown, .28, .2, .06)]
    result = extract(source, definition(partial=True))
    assert unknown in " ".join(item["text"] for item in result["table_unparsed"]["detail"])


@pytest.mark.parametrize("partial", [False, True])
def test_quoted_identifier_without_own_backbone_never_fabricates_a_row(partial):
    source = [*sources(partial=partial), line("REF-12345-99", .05, .42, .20)]
    result = extract(source, definition(partial=partial))
    assert any(item.get("reason") == "DECLARED_START_HAS_NO_OWN_TYPED_BACKBONE"
               for item in result["table_unparsed"]["detail"])
    assert all(not row["description"]["value"].startswith("REF-") for row in result["tables"]["detail"])


def test_repeated_identifier_keeps_two_distinct_source_positions():
    source = sources()
    marker = next(item for item in source if item["text"] == "ABC-12345-02")
    marker["text"] = marker["words"][0]["text"] = "ABC-12345-01"
    result = extract(source, definition())
    assert len(result["tables"]["detail"]) == 2
    assert result["tables"]["detail"][0]["description"]["origin"] != result["tables"]["detail"][1]["description"]["origin"]


def test_boundary_crossing_scalar_does_not_create_a_mixed_row():
    result = extract([*sources(), line("9", .45, .547, .03, .012)], definition())
    assert any(item.get("reason") == "DECLARED_ROW_START_BOUNDARY_AMBIGUOUS"
               for item in result["table_unparsed"]["detail"])


@pytest.mark.parametrize("bad", ["UNKNOWN", True, None])
def test_unknown_identifier_alphabet_is_rejected_by_schema_and_matcher(bad):
    rule = definition()["tables"][0]["visual"]
    rule["row_start"]["segments"][0]["alphabet"] = bad
    with pytest.raises(ValidationError):
        VisualTableRule.model_validate(rule)
    assert not rules.declared_row_start("ABC-12345-01", rule["row_start"])


@pytest.mark.parametrize("patch", [{"column": True}, {"column": 4}, {"separator": ".*"},
                                   {"maximum_start_inset": float("nan")},
                                   {"segments": [{"alphabet": "ASCII_DIGIT", "minimum": 1, "maximum": 2}]}])
def test_identifier_shape_is_bounded(patch):
    rule = definition()["tables"][0]["visual"]
    rule["row_start"].update(patch)
    with pytest.raises(ValidationError):
        VisualTableRule.model_validate(rule)


def test_absent_optins_keep_legacy_python_json_and_roundtrip_exact():
    rule = definition()["tables"][0]["visual"]
    rule.pop("row_start")
    legacy = VisualTableRule.model_validate(rule)
    dumped = legacy.model_dump(mode="json")
    assert not {"row_start", "header_label_aliases", "header_observation_policy"} & dumped.keys()
    assert json.loads(legacy.model_dump_json()) == dumped == legacy.model_dump()
    assert VisualTableRule.model_validate(dumped).model_dump(mode="json") == dumped
    assert legacy.model_dump(include={"row_start", "page"}) == {"page": 1}
    assert legacy.model_dump(exclude={"page"}) == {key: value for key, value in dumped.items() if key != "page"}


def test_declared_literal_alias_only_applies_to_same_column_and_closes_header():
    source, declared = sources(), definition()
    declared["tables"][0]["visual"]["header_label_aliases"] = [[], ["Units"], [], []]
    item = source[1]
    item["text"] = item["words"][0]["text"] = "Units"
    result = extract(source, declared)
    assert len(result["tables"]["detail"]) == 2
    assert result["table_unparsed"]["detail"] == []


@pytest.mark.parametrize("aliases", [[[], ["Amount"], [], []], [[], ["Units", "Units."], [], []],
                                    [[], ["Units"]], [[], [" "] , [], []]])
def test_alias_collisions_duplicates_and_shape_are_rejected(aliases):
    rule = definition()["tables"][0]["visual"]
    rule["header_label_aliases"] = aliases
    with pytest.raises(ValidationError):
        VisualTableRule.model_validate(rule)


def test_complete_canonical_header_keeps_values_and_origins_when_alias_exists():
    declared = definition()
    declared["tables"][0]["visual"].pop("row_start")
    source = sources()
    before = extract(source, declared)
    declared["tables"][0]["visual"]["header_label_aliases"] = [[], ["Units"], [], []]
    assert extract(source, declared) == before


def test_partial_header_requires_typed_support_and_rejects_duplicate_anchors():
    declared = definition(partial=True)
    for field in declared["tables"][0]["fields"]:
        field["logical_type"] = "STRING"
    assert not extract(sources(partial=True), declared)["tables"]["detail"]
    result = extract([*sources(partial=True), line("Amount", .82, .2)], definition(partial=True))
    assert not result["tables"]["detail"]
    assert result["table_unparsed"]["detail"]


def continuation_source():
    source = [*sources(), *sources(page=2)]
    source += [line("Invoice: INV-000001", .05, .1, .25),
               line("Example", .05, .03, page=2), line("INV-000001", .45, .03, page=2),
               line("CONTINUATI?N", .82, .03, .13, page=2)]
    declared = definition(partial=True)
    declared["fields"] = [{"name": "invoice", "label": "Invoice", "pattern": "(INV-[0-9]+)",
                           "logical_type": "STRING", "required": True}]
    declared["tables"][0]["visual"]["continuation_context"] = {
        "region": [0, .01, 1, .1], "label": "CONTINUATION EXAMPLE"}
    return source, declared


def test_corrupt_context_is_visible_unverified_and_bound_to_valid_header_source():
    source, declared = continuation_source()
    result = extract(source, declared)
    regions = result["table_unparsed"]["detail"]
    assert len(result["tables"]["detail"]) == 4 and len(regions) == 1
    region = regions[0]
    assert region["reason"] == "CONTINUATION_CONTEXT_UNVERIFIED"
    assert "CONTINUATI?N" in region["text"]
    assert region["header_observation"]["supported_headers"][0]["value"] == "INV-000001"
    assert rules.valid_header_observation(region, source, declared, "detail")


@pytest.mark.parametrize("change", ["scalar", "identifier", "missing_header", "outside", "all_string"])
def test_context_containing_product_or_without_source_evidence_is_not_confirmable(change):
    source, declared = continuation_source()
    if change == "scalar":
        source.append(line("2", .45, .06, page=2))
    elif change == "identifier":
        source.append(line("SKU-12345-99", .05, .06, .2, page=2))
    elif change == "missing_header":
        source = [item for item in source if not item["text"].startswith("Invoice:")]
    elif change == "outside":
        source.append(line("BOUNDARY", .05, .097, .15, .01, page=2))
    else:
        for field in declared["tables"][0]["fields"]:
            field["logical_type"] = "STRING"
    result = extract(source, declared)
    assert result["table_unparsed"]["detail"]
    assert all("header_observation" not in region for region in result["table_unparsed"]["detail"])


def test_observation_policy_requires_declared_row_identity():
    rule = definition(partial=True)["tables"][0]["visual"]
    rule.pop("row_start")
    with pytest.raises(ValidationError):
        VisualTableRule.model_validate(rule)


def test_line_only_legacy_geometry_cannot_invent_word_observation_indices():
    source = sources(partial=True)
    for item in source:
        item.pop("words")
    result = extract(source, definition(partial=True))
    assert not result["tables"]["detail"]
    assert result["table_unparsed"]["detail"]
    assert all("header_observation" not in item for item in result["table_unparsed"]["detail"])


def test_global_row_limit_also_applies_before_unchanged_primary_return(monkeypatch):
    monkeypatch.setattr(rules, "_legacy_table_candidates", lambda *args: ([[]] * 10001, []))
    with pytest.raises(ValueError, match="DOCUMENT_ROW_LIMIT"):
        rules.table_candidates([], definition()["tables"][0]["visual"])


def test_global_row_limit_cannot_reset_on_a_new_page_or_segment(monkeypatch):
    declared = definition()
    table = declared["tables"][0]
    source = [*sources(), *sources(page=2)]
    unknown = [{"page": page, "region": [.05, .3, .95, .7], "text": "visible body", "method": "OCR"}
               for page in [1, 2]]
    monkeypatch.setattr(rules, "_legacy_table_candidates", lambda *args: ([], unknown))
    monkeypatch.setattr(rules, "_declared_segment", lambda *args: ([[]] * 6000, [], False))
    with pytest.raises(ValueError, match="DOCUMENT_ROW_LIMIT"):
        rules.table_candidates(source, table["visual"], table["fields"], (), lambda *_: (None, "INVALID"))


def test_complete_primary_page_keeps_every_value_and_origin_with_new_optins():
    declared = definition()
    source = sources()
    for item in source:
        if "Service" in item["text"]:
            item["region"][1] -= .015
            item["region"][3] -= .015
            item["words"][0]["region"] = item["region"].copy()
        elif item["text"] in {"2", "3", "17.50", "35.00", "52.50"}:
            item["region"][1] -= .03
            item["region"][3] -= .03
            item["words"][0]["region"] = item["region"].copy()
    old = copy.deepcopy(declared)
    old["tables"][0]["visual"].pop("row_start")
    assert extract(source, declared) == extract(source, old)


def test_executor_rejects_initial_review_resolution_and_mutated_observation():
    from trackvance.document_builder_executor import _valid_result

    source, declared = sources(partial=True), definition(partial=True)
    payload = extract(source, declared)
    request = {"operation": "EXTRACT", "definition": declared,
               "references": [{"id": "generic-reference", "lines": source}]}
    assert _valid_result({"generic-reference": payload}, request)
    altered = copy.deepcopy(payload)
    altered["header_resolutions"] = []
    assert not _valid_result({"generic-reference": altered}, request)
    altered = copy.deepcopy(payload)
    altered["table_unparsed"]["detail"][0]["header_observation"]["source_words"][0]["text"] = "OTHER"
    assert not _valid_result({"generic-reference": altered}, request)


@pytest.mark.parametrize("change", ["row", "cell", "origin", "region", "resolved", "geometry"])
def test_confined_automatic_recheck_rejects_omission_or_changed_source(change, monkeypatch):
    from trackvance import document_builder_executor as executor

    source, declared = sources(partial=True), definition(partial=True)
    actual = extract(source, declared)
    calls = []

    def confined(operation, references, definition):
        calls.append((operation, references, definition))
        assert operation == "EXTRACT" and references[0]["lines"] == source
        return {references[0]["id"]: actual}

    monkeypatch.setattr(executor, "run_builder", confined)
    assert executor.verify_automatic_payload(actual, declared)
    altered = copy.deepcopy(actual)
    if change == "row":
        altered["tables"]["detail"].pop()
    elif change == "cell":
        altered["tables"]["detail"][0]["quantity"]["value"] = "9"
    elif change == "origin":
        altered["tables"]["detail"][0]["quantity"]["origin"] = None
    elif change == "region":
        altered["table_unparsed"]["detail"] = []
    elif change == "geometry":
        altered["page_geometry"] = {"1": {"width": 1, "height": 1, "unit": "PDF_POINT"}}
    else:
        altered["header_resolutions"] = []
    assert not executor.verify_automatic_payload(altered, declared)
    assert len(calls) == (1 if change == "resolved" else 2)


def test_automatic_recheck_cannot_turn_child_failure_into_approval(monkeypatch):
    from trackvance import document_builder_executor as executor

    def failed(*args):
        raise ValueError("DOCUMENT_BUILDER_TIMEOUT")

    monkeypatch.setattr(executor, "run_builder", failed)
    with pytest.raises(ValueError, match="DOCUMENT_BUILDER_TIMEOUT"):
        executor.verify_automatic_payload(extract(sources(partial=True), definition(partial=True)), definition(partial=True))


@pytest.mark.parametrize("context", [False, True])
def test_parent_transport_never_repeats_semantic_header_search(context, monkeypatch):
    source, declared = continuation_source() if context else (sources(partial=True), definition(partial=True))
    region = extract(source, declared)["table_unparsed"]["detail"][0]

    def forbidden(*args, **kwargs):
        raise AssertionError("Semantic search must remain in the confined EXTRACT worker")

    monkeypatch.setattr(rules, "partial_header_candidates", forbidden)
    monkeypatch.setattr(rules, "continuation_observations", forbidden)
    assert rules.valid_header_observation(region, source, declared, "detail")


def test_executor_hashes_raw_and_definition_once_for_multiple_observations(monkeypatch):
    from trackvance.document_builder_executor import _valid_result

    source = [*sources(partial=True), *sources(partial=True, page=2)]
    declared = definition(partial=True)
    payload = extract(source, declared)
    assert len(payload["table_unparsed"]["detail"]) == 2
    counted = {"source": 0, "definition": 0}
    original = rules.source_digest

    def digest(value):
        if value is source:
            counted["source"] += 1
        if value is declared:
            counted["definition"] += 1
        return original(value)

    monkeypatch.setattr(rules, "source_digest", digest)
    request = {"operation": "EXTRACT", "definition": declared,
               "references": [{"id": "generic-reference", "lines": source}]}
    assert _valid_result({"generic-reference": payload}, request)
    assert counted == {"source": 1, "definition": 1}
