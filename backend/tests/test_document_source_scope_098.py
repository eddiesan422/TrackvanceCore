"""Small software regressions; synthetic source spans, no OCR/model/oracle."""
import ast
import copy
import importlib.util
import math
import os
import sys
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = Path(os.environ.get("FIELD_SCOPE_SOURCE", str(HERE.parent / "src/trackvance")))
BASE = HERE.parent / "src/trackvance"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


package = types.ModuleType("_field_scope_source_fixture")
package.__path__ = []
sys.modules[package.__name__] = package
fragments = load(package.__name__ + ".document_semantic_fragments", BASE / "document_semantic_fragments.py")
rules = load(package.__name__ + ".document_template_builder_rules", CODE / "document_template_builder_rules.py")
literal = load(package.__name__ + ".document_literal_tables", BASE / "document_literal_tables.py")
engine = load("_field_scope_engine_fixture", CODE / "document_engine.py")
namespace = {"copy": copy, "math": math, "validate_value": engine.validate_value,
    "arithmetic_checks": engine.arithmetic_checks, "cross_table_checks": engine.cross_table_checks,
    "prompt_catalog_view": literal.prompt_catalog_view}
namespace.update({name: getattr(fragments, name) for name in ("build_catalog", "multiline_fields",
    "ESCAPED_LINE_BREAK", "escaped_break_normalization", "canonical_observed")})
operation = ast.parse((BASE / "operations_common.py").read_text(encoding="utf-8"))
error = next(node for node in operation.body if isinstance(node, ast.ClassDef) and node.name == "OperationError")
exec(compile(ast.Module(body=[error], type_ignores=[]), "source-operation-error", "exec"), namespace)  # noqa: S102 -- fixed trusted class AST, no input code.
selected = {"_semantic_cell", "semantic_targets", "apply_fragment_semantics", "apply_semantics",
    "_merge_semantic_cells", "_refresh_checks", "_fragment_source_origin", "_field_source_scope", "_region", "_contains"}
source = ast.parse((CODE / "document_local_inference.py").read_text(encoding="utf-8"))
functions = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in selected]
exec(compile(ast.Module(body=functions, type_ignores=[]), "actual-source-semantic-functions", "exec"), namespace)  # noqa: S102 -- fixed trusted function allowlist, no input code.


def line(text, x0, y0, x1, y1, *, page=1, method="OCR", words=None):
    result = {"text": text, "page": page, "region": [x0, y0, x1, y1], "method": method, "confidence": None}
    if words is not None:
        result["words"] = words
    return result


def field(name, bounds):
    return {"name": name, "label": "Tax ID", "logical_type": "STRING", "required": True,
        "pattern": "(.+)", "visual": {"kind": "LABEL_VALUE", "page": 1, "region": [.04, .10, .95, .40],
            "label": "Tax ID", "direction": "BELOW", "tolerance": .12, "section_bounds": bounds,
            "value_pattern": r"([^ ]+@[^ ]+\.[A-Za-z]{2,})", "baseline_tolerance": .008}}


def definition():
    return {"fields": [field("sender_contact", [.04, .45]), field("customer_contact", [.55, .95])],
        "tables": [], "arithmetic": []}


def source_lines():
    return [line("Tax ID: A-111", .08, .18, .25, .20),
        line("sender@issuer.invalid", .08, .23, .32, .25),
        line("Tax ID: B-222", .60, .18, .78, .20),
        line("buyer@recipient.invalid", .60, .23, .86, .25)]


def semantic_result(lines, selections, declared=None, *, v1=False):
    declared = definition() if declared is None else declared
    payload = engine.extract([], definition())
    catalog = fragments.build_catalog(lines)
    if v1:
        candidate = {"fields": {name: {"observed": lines[index]["text"], "evidence_id": index}
            for name, index in selections.items()}, "tables": {}}
        return namespace["apply_semantics"](payload, candidate, declared, lines)
    candidate = {"fields": {name: {"fragment_id": next(item["fragment_id"] for item in catalog["fragments"]
        if item["evidence_id"] == index and item["start"] == 0 and item["end"] == len(lines[index]["text"]))}
        for name, index in selections.items()}, "tables": {}}
    return namespace["apply_fragment_semantics"](payload, candidate, declared, lines, catalog,
        namespace["semantic_targets"](payload, declared))


def selected_spans_result(lines, declared, selections):
    payload = engine.extract([], declared)
    catalog = fragments.build_catalog(lines)
    fields = {field["name"]: {"fragment_id": None} for field in declared["fields"]}
    for name, (evidence, start, end) in selections.items():
        fields[name] = {"fragment_id": next(item["fragment_id"] for item in catalog["fragments"]
            if (item["evidence_id"], item["start"], item["end"]) == (evidence, start, end))}
    candidate = {"fields": fields, "tables": {table["name"]: [] for table in declared["tables"]}}
    return namespace["apply_fragment_semantics"](payload, candidate, declared, lines, catalog,
        namespace["semantic_targets"](payload, declared))


class SourceScopeTests(unittest.TestCase):
    def test_coarse_below_label_delimiter_recovers_source_with_original_block_geometry(self):
        observations = source_lines()
        before = copy.deepcopy(observations)
        result = engine.extract(observations, definition())
        for name, index in (("sender_contact", 1), ("customer_contact", 3)):
            cell = result["fields"][name]
            self.assertEqual((cell["value"], cell["observed"], cell["validity"]),
                (observations[index]["text"], observations[index]["text"], "VALID"))
            self.assertEqual(cell["origin"], {"page": 1, "region": observations[index]["region"],
                "method": "OCR", "resolution": "LAYOUT_BLOCK"})
            self.assertFalse(cell["reviewed"])
        self.assertEqual(observations, before)

    def test_v2_same_shape_party_swap_is_not_a_valid_header(self):
        observations = source_lines()
        result = semantic_result(observations, {"sender_contact": 3, "customer_contact": 1})
        for name, index in (("sender_contact", 3), ("customer_contact", 1)):
            cell = result["fields"][name]
            self.assertEqual(cell["validity"], "INVALID")
            self.assertIsNone(cell["value"])
            self.assertEqual(cell["observed"], observations[index]["text"])
            self.assertEqual(cell["origin"]["region"], observations[index]["region"])
            self.assertEqual(cell["source_scope_issue"], "OUTSIDE_DECLARED_FIELD_SCOPE")
            self.assertIn("source_fragment", cell)
            self.assertFalse(cell["reviewed"])

    def test_v2_matching_scope_retains_exact_literal_and_source_fragment(self):
        observations = source_lines()
        result = semantic_result(observations, {"sender_contact": 1, "customer_contact": 3})
        for name, index in (("sender_contact", 1), ("customer_contact", 3)):
            self.assertEqual(result["fields"][name]["value"], observations[index]["text"])
            self.assertEqual(result["fields"][name]["validity"], "VALID")
            self.assertEqual(result["fields"][name]["origin"]["resolution"], "LAYOUT_BLOCK")

    def test_identical_literal_at_other_party_location_still_fails(self):
        observations = source_lines()
        observations[1]["text"] = observations[3]["text"] = "shared@both.invalid"
        result = semantic_result(observations, {"sender_contact": 3, "customer_contact": 1})
        self.assertTrue(all(cell["validity"] == "INVALID" for cell in result["fields"].values()))

    def test_page_roi_and_coarse_crossing_are_not_accepted_by_text_identity(self):
        for mutation in ("page", "roi", "crossing", "degenerate", "nan"):
            with self.subTest(mutation=mutation):
                observations = source_lines()
                if mutation == "page": observations[1]["page"] = 2
                elif mutation == "roi": observations[1]["region"][1:4:2] = [.50, .52]
                elif mutation == "crossing": observations[1]["region"][2] = .60
                elif mutation == "degenerate": observations[1]["region"][2] = observations[1]["region"][0]
                else: observations[1]["region"][2] = float("nan")
                if mutation == "nan":
                    with self.assertRaisesRegex(ValueError, "LLM_FRAGMENT_INTEGRITY"):
                        semantic_result(observations, {"sender_contact": 1, "customer_contact": 3})
                    continue
                result = semantic_result(observations, {"sender_contact": 1, "customer_contact": 3})
                self.assertEqual(result["fields"]["sender_contact"]["validity"], "INVALID")

    def test_declared_visual_alternative_is_allowed_but_not_arbitrary_location(self):
        observations = source_lines()
        observations[1]["page"] = 2
        declared = definition()
        alternative = copy.deepcopy(declared["fields"][0]["visual"])
        alternative["page"] = 2
        declared["fields"][0]["visual_alternatives"] = [alternative]
        self.assertEqual(semantic_result(observations, {"sender_contact": 1, "customer_contact": 3}, declared)
            ["fields"]["sender_contact"]["validity"], "VALID")

    def test_invalid_declared_scope_is_not_a_whole_page_fallback(self):
        for bounds in ([.6, .2], [False, .5], [0, float("inf")], "left"):
            with self.subTest(bounds=bounds):
                declared = definition()
                declared["fields"][0]["visual"]["section_bounds"] = bounds
                result = semantic_result(source_lines(), {"sender_contact": 1, "customer_contact": 3}, declared)
                self.assertEqual(result["fields"]["sender_contact"]["validity"], "INVALID")

    def test_coarse_anchor_needs_exact_label_delimiter_and_ocr_below_mode(self):
        for text, method, direction in (("Tax Identity: A-111", "OCR", "BELOW"),
                ("Tax ID A-111", "OCR", "BELOW"), ("Tax ID: A-111", "DIGITAL", "BELOW"),
                ("Tax ID: A-111", "OCR", "RIGHT")):
            with self.subTest(text=text, method=method, direction=direction):
                observations = source_lines()
                observations[0].update(text=text, method=method)
                declared = definition()
                declared["fields"][0]["visual"]["direction"] = direction
                result = engine.extract(observations, declared)
                self.assertEqual(result["fields"]["sender_contact"]["validity"], "MISSING")

    def test_label_block_width_never_becomes_an_invented_value_rectangle(self):
        observations = source_lines()
        observations[0].update(text="Tax ID: A-111 WITH LONG SOURCE BLOCK", region=[.06, .18, .44, .20])
        before = copy.deepcopy(observations)
        cell = engine.extract(observations, definition())["fields"]["sender_contact"]
        self.assertEqual(cell["origin"]["region"], observations[1]["region"])
        self.assertEqual(cell["origin"]["resolution"], "LAYOUT_BLOCK")
        self.assertEqual(observations, before)

    def test_no_g_to_at_or_other_literal_repair_is_performed(self):
        observations = source_lines()
        observations[1]["text"] = "senderGissuer.invalid"
        self.assertEqual(engine.extract(observations, definition())["fields"]["sender_contact"]["validity"], "MISSING")

    def test_v1_semantic_path_is_unchanged(self):
        result = semantic_result(source_lines(), {"sender_contact": 3, "customer_contact": 1}, v1=True)
        self.assertEqual(result["fields"]["sender_contact"]["value"], "buyer@recipient.invalid")
        self.assertEqual(result["fields"]["sender_contact"]["validity"], "VALID")

    def test_existing_valid_header_is_retained_and_cannot_be_semantically_overwritten(self):
        observations, declared = source_lines(), definition()
        payload = engine.extract(observations, declared)
        before = copy.deepcopy(payload)
        catalog = fragments.build_catalog(observations)
        targets = namespace["semantic_targets"](payload, declared)
        candidate = {"fields": {item["name"]: {"fragment_id": None} for item in declared["fields"]}, "tables": {}}
        result = namespace["apply_fragment_semantics"](payload, candidate, declared, observations, catalog, targets)
        self.assertEqual(result["fields"], before["fields"])
        candidate["fields"]["sender_contact"]["fragment_id"] = catalog["fragments"][0]["fragment_id"]
        with self.assertRaises(namespace["OperationError"]):
            namespace["apply_fragment_semantics"](payload, candidate, declared, observations, catalog, targets)
        self.assertEqual(payload, before)

    def test_no_visual_declaration_retains_existing_literal_contract(self):
        declared = definition()
        declared["fields"][0].pop("visual")
        self.assertEqual(semantic_result(source_lines(), {"sender_contact": 3, "customer_contact": 1}, declared)
            ["fields"]["sender_contact"]["validity"], "VALID")

    def test_scope_remains_invalid_after_escaped_line_break_normalization(self):
        declared = {"fields": [{"name": "note", "logical_type": "STRING", "visual": {
            "kind": "REGION_CONTEXT", "page": 1, "region": [.04, .1, .45, .4]}}],
            "tables": [{"name": "headers", "fields": [{"name": "note", "logical_type": "STRING"}],
                "visual": {"page": 1, "region": [.02, .6, .98, .65], "header_labels": ["NOTE"],
                    "column_bounds": [.02, .98], "multiline_column": 0}}]}
        raw = r"alpha\nbeta"
        observations = [line(raw, .6, .23, .86, .25)]
        before = copy.deepcopy(observations)
        cell = selected_spans_result(observations, declared, {"note": (0, 0, len(raw))})["fields"]["note"]
        self.assertEqual(cell["validity"], "INVALID")
        self.assertIsNone(cell["value"])
        self.assertEqual(cell["observed"], raw)
        self.assertEqual(cell["source_scope_issue"], "OUTSIDE_DECLARED_FIELD_SCOPE")
        self.assertIn("source_text_normalization", cell)
        self.assertFalse(cell["reviewed"])
        self.assertEqual(observations, before)

    def test_fine_source_word_inside_roi_is_not_rejected_by_cross_party_line_box(self):
        first, second = "sender@issuer.invalid", "buyer@recipient.invalid"
        observations = [line(first + " " + second, .08, .23, .86, .25, words=[
            {"text": first, "region": [.08, .23, .32, .25]},
            {"text": second, "region": [.60, .23, .86, .25]}])]
        before = copy.deepcopy(observations)
        result = selected_spans_result(observations, definition(), {
            "sender_contact": (0, 0, len(first)), "customer_contact": (0, len(first) + 1, len(first) + 1 + len(second))})
        for name, index in (("sender_contact", 0), ("customer_contact", 1)):
            cell = result["fields"][name]
            self.assertEqual(cell["validity"], "VALID")
            self.assertEqual(cell["origin"]["region"], observations[0]["words"][index]["region"])
            self.assertEqual(cell["origin"]["source_word_indexes"], [index])
            self.assertEqual(cell["origin"]["resolution"], "MEASURED_WORDS")
        self.assertEqual(observations, before)

    def test_repeated_identical_literals_use_selected_offsets_not_first_text_match(self):
        raw = "shared@both.invalid"
        observations = [line(raw + " " + raw, .08, .23, .86, .25, words=[
            {"text": raw, "region": [.08, .23, .32, .25]},
            {"text": raw, "region": [.60, .23, .86, .25]}])]
        first, second = (0, 0, len(raw)), (0, len(raw) + 1, 2 * len(raw) + 1)
        correct = selected_spans_result(observations, definition(), {"sender_contact": first, "customer_contact": second})
        self.assertTrue(all(cell["validity"] == "VALID" for cell in correct["fields"].values()))
        swapped = selected_spans_result(observations, definition(), {"sender_contact": second, "customer_contact": first})
        for name, index in (("sender_contact", 1), ("customer_contact", 0)):
            cell = swapped["fields"][name]
            self.assertEqual(cell["validity"], "INVALID")
            self.assertIsNone(cell["value"])
            self.assertEqual(cell["observed"], raw)
            self.assertEqual(cell["origin"]["source_word_indexes"], [index])

    def test_unknown_word_alignment_or_invalid_word_box_cannot_narrow_source(self):
        raw = "sender@issuer.invalid"
        for failure in ("text", "outside", "degenerate"):
            with self.subTest(failure=failure):
                words = [{"text": raw, "region": [.08, .23, .32, .25]},
                         {"text": "OTHER", "region": [.60, .23, .86, .25]}]
                if failure == "text": words[1]["text"] = "DIFFERENT"
                elif failure == "outside": words[0]["region"][0] = .01
                else: words[0]["region"][2] = words[0]["region"][0]
                observations = [line(raw + " OTHER", .08, .23, .86, .25, words=words)]
                cell = selected_spans_result(observations, definition(), {"sender_contact": (0, 0, len(raw))})["fields"]["sender_contact"]
                self.assertEqual(cell["validity"], "INVALID")
                self.assertEqual(cell["origin"]["region"], observations[0]["region"])
                self.assertEqual(cell["origin"]["resolution"], "LAYOUT_BLOCK")
                self.assertNotIn("source_word_indexes", cell["origin"])

    def test_source_subword_preserves_full_measured_block_instead_of_interpolating(self):
        raw = "prefix:sender@issuer.invalid"
        observations = [line(raw, .08, .23, .32, .25, words=[
            {"text": raw, "region": [.08, .23, .32, .25]}])]
        cell = selected_spans_result(observations, definition(), {"sender_contact": (0, len("prefix:"), len(raw))})["fields"]["sender_contact"]
        self.assertEqual(cell["validity"], "VALID")
        self.assertEqual(cell["origin"]["region"], observations[0]["region"])
        self.assertEqual(cell["origin"]["resolution"], "LAYOUT_BLOCK")
        self.assertNotIn("source_word_indexes", cell["origin"])

    def test_multiword_fragment_origin_is_union_of_actual_selected_word_boxes(self):
        declared = {"fields": [{"name": "name", "logical_type": "STRING", "visual": {
            "kind": "REGION_CONTEXT", "page": 1, "region": [.04, .1, .45, .4]}}], "tables": []}
        observations = [line("ACME LTD | OTHER", .08, .23, .86, .26, words=[
            {"text": "ACME", "region": [.08, .23, .18, .25]},
            {"text": "LTD", "region": [.20, .24, .29, .26]},
            {"text": "|", "region": [.48, .23, .49, .25]},
            {"text": "OTHER", "region": [.60, .23, .86, .25]}])]
        cell = selected_spans_result(observations, declared, {"name": (0, 0, len("ACME LTD"))})["fields"]["name"]
        self.assertEqual(cell["validity"], "VALID")
        self.assertEqual(cell["origin"]["region"], [.08, .23, .29, .26])
        self.assertEqual(cell["origin"]["source_word_indexes"], [0, 1])


if __name__ == "__main__":
    unittest.main()
