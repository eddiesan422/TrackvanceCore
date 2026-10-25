"""Synthetic source/Decimal/ownership negatives, no OCR/model/API or oracle."""
import copy
import unittest

from trackvance import document_engine as ENGINE
from trackvance.document_candidate_arbitration import (
    _declared_checks,
    _invoice_row_algebra,
    control_binding,
    digest,
    reconcile_candidates,
)

SOURCE = "a" * 64
CONFIGURATION = "b" * 64


def cell(raw, field, box, *, validity=None):
    value, status = ENGINE.validate_value(raw, field)
    return {"observed": raw, "value": value, "validity": validity or status, "reviewed": False, "confidence": None,
            "origin": {"page": 1, "region": box, "method": "OCR"} if raw is not None else None}


def fixture():
    fields = [{"name": "item", "logical_type": "STRING"},
              {"name": "qty", "logical_type": "DECIMAL", "decimal_separator": ",", "thousands_separator": "."},
              {"name": "amount", "logical_type": "DECIMAL", "decimal_separator": ",", "thousands_separator": "."}]
    grammar = {"column": 0, "separator": "-", "segments": [
        {"alphabet": "ASCII_ALPHA", "minimum": 2, "maximum": 4},
        {"alphabet": "ASCII_DIGIT", "minimum": 5, "maximum": 5},
        {"alphabet": "ASCII_DIGIT", "minimum": 2, "maximum": 2}], "maximum_start_inset": .035}
    definition = {"fields": [], "tables": [{"name": "detail", "fields": fields,
        "visual": {"page": 1, "row_start": grammar, "column_bounds": [.02, .55, .7, .98], "flowing": True,
                   "stop_label": "END OF DETAIL"}}]}
    before, after, lines = [], [], []
    for number, top in enumerate((.32, .55)):
        key = f"AB-{100 + number:05d}-01"
        row = {"item": cell(key + " Printed work", fields[0], [.03, top, .4, top + .045]),
               "qty": cell("3,50", fields[1], [.57, top + .025, .64, top + .045]),
               "amount": cell("27,65", fields[2], [.83, top + .025, .94, top + .045])}
        after.append(row)
        old = {"item": cell(key + "\\Printed work", fields[0], [.02, .3, .98, .7]),
               "qty": cell(None, fields[1], None), "amount": cell("2,00", fields[2], [.02, .3, .98, .7])}
        before.append(old)
        words = [{"text": key, "region": [.03, top, .2, top + .015]},
                 {"text": "Printed work", "region": [.03, top + .025, .4, top + .045]},
                 {"text": "3,50", "region": [.57, top + .025, .64, top + .045]},
                 {"text": "27,65", "region": [.83, top + .025, .94, top + .045]}]
        lines.append({"page": 1, "region": [.03, top, .94, top + .045], "text": key + " Printed work 3,50 27,65",
                      "method": "OCR", "words": words})
    lines.append({"page": 1, "region": [.02, .8, .5, .82], "text": "END OF DETAIL | CONTROL COPY", "method": "OCR",
                  "words": [{"text": "END OF DETAIL | CONTROL COPY", "region": [.02, .8, .5, .82]}]})
    active = copy.deepcopy(before)
    for row in active:
        for candidate in row.values():
            candidate["reextraction_conflict"] = True
            candidate["alternatives"] = [candidate["observed"]] if candidate["observed"] is not None else []
            if candidate["observed"] is not None:
                candidate["validity"] = "AMBIGUOUS"
    data = {"payload": {"fields": {}, "tables": {"detail": active}, "lines": lines, "pages": [1],
        "table_unparsed": {"detail": [{"reason": "HEADER_UNVERIFIED"}]},
        "semantic_conflicts": ["REEXTRACTION_TABLE:detail"],
        "reextraction_table_history": {"detail": [{"before": before, "after": after, "reason": "ROW_COUNT_OR_IDENTITY_ORDER"}]}},
        "engine": {"configuration_sha256": CONFIGURATION, "independent_row_coverage": False}}
    independent = {"payload": {"fields": {}, "tables": {"detail": copy.deepcopy(after)}, "lines": copy.deepcopy(lines),
                               "pages": [1], "table_unparsed": {"detail": []}},
                   "engine": {"key": "POPPLER_TESSERACT_CPU_V1"}}
    return data, independent, definition


def run(data, independent, definition, **overrides):
    args = {"source_sha256": SOURCE, "configuration_sha256": CONFIGURATION,
            "worker_control_binding": control_binding(independent, SOURCE, definition, CONFIGURATION),
            "validator": ENGINE.validate_value, "declared_row_start": ENGINE._visual_rules.declared_row_start,
            "declared_table_stop": ENGINE._visual_rules.declared_table_stop,
            "declared_checks": lambda payload, frozen: _declared_checks(payload, frozen),
            "row_algebra_checks": lambda payload, frozen: _invoice_row_algebra(payload, frozen)}
    args.update(overrides)
    return reconcile_candidates(data, independent, definition, **args)


class IndependentCellArbitrationTests(unittest.TestCase):
    def test_positive_same_source_unique_keys_exact_literals_and_measured_cells(self):
        data, independent, definition = fixture()
        old, control = copy.deepcopy(data), copy.deepcopy(independent)
        result, report = run(data, independent, definition)
        self.assertEqual(len(report["selected"]), 4)
        self.assertEqual([row["qty"]["value"] for row in result["payload"]["tables"]["detail"]], ["3.50", "3.50"])
        self.assertEqual(data, old)
        self.assertEqual(independent, control)
        self.assertEqual(result["payload"]["reextraction_table_history"], old["payload"]["reextraction_table_history"])
        self.assertEqual(result["payload"]["semantic_conflicts"], old["payload"]["semantic_conflicts"])
        self.assertEqual(result["payload"]["table_unparsed"], old["payload"]["table_unparsed"])
        self.assertIs(result["engine"]["independent_row_coverage"], False)
        self.assertTrue(all(not item["reviewed"] for row in result["payload"]["tables"]["detail"] for item in row.values()))
        self.assertFalse(report["automatic_approval_authority"])
        self.assertFalse(report["coverage_authority"])
        self.assertTrue(report["requires_recertification"])
        self.assertEqual(report["selected"][0]["control_words"][0]["text"], "3,50")

    def test_worker_binding_rejects_source_definition_configuration_or_control_change(self):
        for name in ("source_sha256", "definition_sha256", "configuration_sha256", "independent_sha256"):
            with self.subTest(name=name):
                data, independent, definition = fixture()
                binding = control_binding(independent, SOURCE, definition, CONFIGURATION)
                binding[name] = "c" * 64
                result, report = run(data, independent, definition, worker_control_binding=binding)
                self.assertEqual(result, data)
                self.assertFalse(report["selected"])

    def test_exact_literal_disagreement_is_not_value_only_rescue(self):
        data, independent, definition = fixture()
        independent["payload"]["tables"]["detail"][0]["qty"].update(observed="3,5", value="3.5")
        independent["payload"]["lines"][0]["words"][2]["text"] = "3,5"
        result, report = run(data, independent, definition)
        self.assertIsNone(result["payload"]["tables"]["detail"][0]["qty"]["value"])
        self.assertTrue(any(item["reason"] == "INDEPENDENT_EXACT_LITERAL_OR_VALUE_DISAGREEMENT" for item in report["rejected"]))

    def test_duplicate_business_key_in_any_read_rejects_whole_table(self):
        for target in ("prior", "new", "control"):
            with self.subTest(target=target):
                data, independent, definition = fixture()
                rows = {"prior": data["payload"]["tables"]["detail"],
                        "new": data["payload"]["reextraction_table_history"]["detail"][0]["after"],
                        "control": independent["payload"]["tables"]["detail"]}[target]
                rows[1]["item"] = copy.deepcopy(rows[0]["item"])
                result, report = run(data, independent, definition)
                self.assertEqual(result, data)
                self.assertFalse(report["selected"])

    def test_row_order_change_never_uses_ordinal_alignment(self):
        data, independent, definition = fixture()
        independent["payload"]["tables"]["detail"].reverse()
        result, report = run(data, independent, definition)
        self.assertEqual(result, data)
        self.assertFalse(report["selected"])

    def test_missing_independent_source_word_blocks_that_cell(self):
        data, independent, definition = fixture()
        independent["payload"]["lines"][0]["words"][2]["text"] = "8,50"
        result, _ = run(data, independent, definition)
        self.assertIsNone(result["payload"]["tables"]["detail"][0]["qty"]["value"])

    def test_substring_does_not_prove_literal(self):
        data, independent, definition = fixture()
        independent["payload"]["lines"][0]["words"][2]["text"] = "13,50"
        result, report = run(data, independent, definition)
        self.assertIsNone(result["payload"]["tables"]["detail"][0]["qty"]["value"])
        self.assertTrue(any(item["reason"] == "INDEPENDENT_LITERAL_SOURCE_EVIDENCE_UNPROVEN" for item in report["rejected"]))

    def test_wrong_column_cannot_be_rescued_by_matching_literal(self):
        data, independent, definition = fixture()
        for target in (data["payload"]["reextraction_table_history"]["detail"][0]["after"],
                       independent["payload"]["tables"]["detail"]):
            target[0]["qty"]["origin"]["region"] = [.73, .345, .8, .365]
        for payload in (data["payload"], independent["payload"]):
            payload["lines"][0]["words"][2]["region"] = [.73, .345, .8, .365]
        result, report = run(data, independent, definition)
        self.assertIsNone(result["payload"]["tables"]["detail"][0]["qty"]["value"])
        self.assertTrue(any(item["reason"] == "SOURCE_ROW_OR_COLUMN_GEOMETRY_UNPROVEN" for item in report["rejected"]))

    def test_same_value_already_corroborated_is_preserved_even_if_flagged(self):
        data, independent, definition = fixture()
        for rows in (data["payload"]["tables"]["detail"], data["payload"]["reextraction_table_history"]["detail"][0]["before"]):
            rows[0]["amount"].update(observed="27,65", value="27.65")
        original = copy.deepcopy(data["payload"]["tables"]["detail"][0]["amount"])
        result, report = run(data, independent, definition)
        self.assertEqual(result["payload"]["tables"]["detail"][0]["amount"], original)
        self.assertTrue(any(item["reason"] == "PRIOR_VALUE_INDEPENDENTLY_CORROBORATED" for item in report["rejected"]))

    def test_human_revision_is_never_changed(self):
        data, independent, definition = fixture()
        data["payload"]["tables"]["detail"][0]["qty"]["reviewed"] = True
        result, report = run(data, independent, definition)
        self.assertEqual(result, data)
        self.assertFalse(report["selected"])

    def test_partial_checkpoint_and_provider_positive_flag_grant_no_authority(self):
        for key, value in (("inference_completion", {"status": "PARTIAL"}), ("independent_cell_arbitration", {"approved": True})):
            with self.subTest(key=key):
                data, independent, definition = fixture()
                data["engine"][key] = value
                result, report = run(data, independent, definition)
                self.assertEqual(result, data)
                self.assertFalse(report["selected"])

    def test_prior_literal_or_origin_cannot_change_outside_history(self):
        data, independent, definition = fixture()
        data["payload"]["tables"]["detail"][0]["amount"]["observed"] = "9,00"
        result, report = run(data, independent, definition)
        self.assertEqual(result, data)
        self.assertFalse(report["selected"])

    def test_coarse_whole_row_is_not_measured_cell_proof(self):
        data, independent, definition = fixture()
        data["payload"]["reextraction_table_history"]["detail"][0]["after"][0]["qty"]["origin"]["region"] = [.02, .3, .98, .7]
        result, _ = run(data, independent, definition)
        self.assertIsNone(result["payload"]["tables"]["detail"][0]["qty"]["value"])

    def test_callback_deadline_cancellation_and_integrity_failures_always_propagate(self):
        class CheckFailure(Exception):
            pass

        data, independent, definition = fixture()
        original = copy.deepcopy(data)
        calls = 0

        def fail_during_cells():
            nonlocal calls
            calls += 1
            if calls == 5:
                raise CheckFailure("explicit failure")

        with self.assertRaises(CheckFailure):
            run(data, independent, definition, check=fail_during_cells)
        self.assertEqual(data, original)

    def test_control_hash_cannot_be_a_provider_assertion_only(self):
        data, independent, definition = fixture()
        binding = control_binding(independent, SOURCE, definition, CONFIGURATION)
        independent["payload"]["lines"][0]["words"][2]["region"][0] += .01
        result, report = run(data, independent, definition, worker_control_binding=binding)
        self.assertEqual(result, data)
        self.assertNotEqual(binding["independent_sha256"], digest(independent))
        self.assertFalse(report["selected"])

    def test_provider_positive_coverage_flag_never_survives_as_worker_proof(self):
        data, independent, definition = fixture()
        data["engine"]["independent_row_coverage"] = True
        result, report = run(data, independent, definition)
        self.assertTrue(report["selected"])
        self.assertIs(result["engine"]["independent_row_coverage"], False)
        self.assertIs(report["prior_provider_row_coverage_claim"], True)

    def test_ambiguous_independent_key_or_overlapping_physical_keys_block_table(self):
        for kind in ("status", "overlap"):
            with self.subTest(kind=kind):
                data, independent, definition = fixture()
                if kind == "status":
                    independent["payload"]["tables"]["detail"][0]["item"]["validity"] = "AMBIGUOUS"
                else:
                    for payload in (data["payload"], independent["payload"]):
                        payload["lines"][1]["words"][0]["region"][1] = .32
                        payload["lines"][1]["region"][1] = .32
                    for rows in (data["payload"]["reextraction_table_history"]["detail"][0]["after"],
                                 independent["payload"]["tables"]["detail"]):
                        rows[1]["item"]["origin"]["region"][1] = .32
                result, report = run(data, independent, definition)
                self.assertEqual(result, data)
                self.assertFalse(report["selected"])

    def test_same_literal_below_measured_stop_in_both_reads_is_not_detail_evidence(self):
        data, independent, definition = fixture()
        for rows in (data["payload"]["reextraction_table_history"]["detail"][0]["after"],
                     independent["payload"]["tables"]["detail"]):
            rows[1]["qty"]["origin"]["region"] = [.57, .9, .64, .92]
        for payload in (data["payload"], independent["payload"]):
            payload["lines"][1]["words"][2]["region"] = [.57, .9, .64, .92]
            payload["lines"][1]["region"][3] = .92
        result, report = run(data, independent, definition)
        self.assertIsNone(result["payload"]["tables"]["detail"][1]["qty"]["value"])
        self.assertTrue(any(item.get("path") == "detail.1.qty" and item["reason"] == "SOURCE_ROW_OR_COLUMN_GEOMETRY_UNPROVEN"
                            for item in report["rejected"]))

    def test_last_row_without_source_measured_stop_is_preserved(self):
        data, independent, definition = fixture()
        independent["payload"]["lines"].pop()
        result, report = run(data, independent, definition)
        self.assertIsNone(result["payload"]["tables"]["detail"][1]["qty"]["value"])
        self.assertEqual(result["payload"]["tables"]["detail"][1]["amount"]["value"], "2.00")
        self.assertTrue(any(item.get("path") == "detail.0.qty" for item in report["selected"]))

    def test_two_matching_wrong_reads_cannot_break_previously_valid_declared_decimal_row(self):
        data, independent, definition = fixture()
        definition["arithmetic"] = [{"name": "declared_row_equality", "table": "detail", "result": "amount",
            "terms": ["qty"], "operation": "SUM", "scale": 2, "tolerance": "0.00"}]
        for rows in (data["payload"]["tables"]["detail"], data["payload"]["reextraction_table_history"]["detail"][0]["before"]):
            rows[0]["qty"].update(observed="2,00", value="2.00", validity="VALID",
                                  origin={"page": 1, "region": [.02, .3, .98, .7], "method": "OCR"})
        for rows in (data["payload"]["reextraction_table_history"]["detail"][0]["after"],
                     independent["payload"]["tables"]["detail"]):
            rows[0]["amount"].update(observed="2,00", value="2.00")
        for payload in (data["payload"], independent["payload"]):
            payload["lines"][0]["words"][3]["text"] = "2,00"
        result, report = run(data, independent, definition)
        self.assertEqual(result, data)
        self.assertEqual(report["status"], "UNCHANGED_DECLARED_CONTROL_REGRESSION")
        self.assertFalse(report["selected"])
        self.assertTrue(any(item["before"]["status"] == "VALID" and item["after"]["status"] == "DISCREPANCY"
                            for item in report["regressions"]))

    def test_incomplete_row_can_keep_positive_cells_without_hiding_declared_failures(self):
        data, independent, definition = fixture()
        definition["arithmetic"] = [{"name": "declared_row_equality", "table": "detail", "result": "amount",
            "terms": ["qty"], "operation": "SUM", "scale": 2, "tolerance": "0.00"}]
        result, report = run(data, independent, definition)
        self.assertEqual(len(report["selected"]), 4)
        self.assertTrue(all(item["status"] == "NOT_EVALUATED" for item in report["declared_checks_before"]))
        self.assertTrue(all(item["status"] == "DISCREPANCY" for item in report["declared_checks_after"]))
        self.assertEqual(result["payload"]["arithmetic"], report["declared_checks_after"])
        self.assertIs(result["engine"]["independent_row_coverage"], False)

    def test_audit_capacity_reverts_whole_batch_without_new_history_or_selected_cells(self):
        data, independent, definition = fixture()
        data["engine"]["bounded_fixture_padding"] = "x" * (16 * 1024**2 - 12000)
        original_hash = digest(data)
        result, report = run(data, independent, definition)
        self.assertEqual(digest(data), original_hash)
        self.assertEqual(result, data)
        self.assertEqual(report["status"], "UNCHANGED_AUDIT_CAPACITY")
        self.assertFalse(report["selected"])
        self.assertNotIn("independent_cell_arbitration_history", result["payload"])


if __name__ == "__main__":
    unittest.main()
