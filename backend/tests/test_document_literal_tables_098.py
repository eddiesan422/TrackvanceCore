import copy
import hashlib
import unittest
from decimal import Decimal

from trackvance import document_literal_tables as mapper


def definition():
    return {
        "fields": [],
        "tables": [
            {
                "name": "items",
                "fields": [
                    {"name": "description", "logical_type": "STRING"},
                    {"name": "quantity", "logical_type": "DECIMAL"},
                    {"name": "amount", "logical_type": "DECIMAL"},
                ],
                "visual": {
                    "header_labels": ["Item description", "Quantity", "Amount"],
                    "multiline_column": 0,
                    "continue_pages": True,
                    "continuation_context": {
                        "label": "CONTINUED ITEMS",
                        "region": [0.1, 0.05, 0.9, 0.15],
                    },
                    "row_start": {
                        "column": 0,
                        "separator": "-",
                        "segments": [
                            {"alphabet": "ASCII_ALPHA", "minimum": 2, "maximum": 4},
                            {"alphabet": "ASCII_DIGIT", "minimum": 5, "maximum": 5},
                            {"alphabet": "ASCII_DIGIT", "minimum": 2, "maximum": 2},
                        ],
                    },
                },
            }
        ],
    }


def line(text, page=1, region=None, label="table"):
    return {
        "text": text,
        "page": page,
        "region": [0.1, 0.2, 0.9, 0.7] if region is None else region,
        "method": "OCR",
        "confidence": None,
        "block_label": label,
    }


def source():
    return [
        line("Item description | Quantity | Amount"),
        line("AB-12345-01\\nPrinted service | 2 | 30,00"),
    ]


def validator(text, field):
    # Test injection only; actual integration must use existing product validator.
    if text is None:
        return None, "MISSING"
    return (
        (str(Decimal(text.replace(",", "."))), "VALID")
        if field["logical_type"] == "DECIMAL"
        else (text, "VALID")
    )


class MapperTests(unittest.TestCase):
    def test_exact_spans_match_catalog_and_keep_actual_block_origin(self):
        observations = source()
        out = mapper.map_candidates(observations, definition())
        row = out["tables"]["items"][0]["cells"]
        catalog = mapper.FRAGMENTS.build_catalog(observations)
        for cell in row.values():
            fragment = catalog["fragments"][cell["source_fragment"]["fragment_id"]]
            self.assertEqual(
                cell["observed"],
                observations[fragment["evidence_id"]]["text"][fragment["start"] : fragment["end"]],
            )
            self.assertEqual(cell["origin"]["region"], observations[1]["region"])
            self.assertNotIn("words", cell)
            self.assertEqual(cell["validity"], "UNVALIDATED")
            self.assertIsNone(cell["confidence"])
            self.assertFalse(cell["reviewed"])

    def test_explicit_multiline_value_keeps_exact_raw_and_hash_metadata(self):
        cell = mapper.map_candidates(source(), definition())["tables"]["items"][0]["cells"][
            "description"
        ]
        self.assertEqual(cell["observed"], "AB-12345-01\\nPrinted service")
        self.assertEqual(cell["candidate_text"], "AB-12345-01 Printed service")
        self.assertEqual(
            cell["source_text_normalization"]["observed_sha256"],
            hashlib.sha256(cell["observed"].encode()).hexdigest(),
        )

    def test_multiline_in_undeclared_cell_is_rejected(self):
        observations = source()
        observations[1]["text"] = "AB-12345-01 service | 2\\n3 | 30,00"
        out = mapper.map_candidates(observations, definition())
        self.assertFalse(out["tables"]["items"])
        self.assertEqual(out["rejected"]["items"][0]["reason"], "UNDECLARED_MULTILINE_CELL")

    def test_no_undeclared_normalization_when_multiline_disabled(self):
        declared = definition()
        declared["tables"][0]["visual"]["multiline_column"] = None
        out = mapper.map_candidates(source(), declared)
        self.assertFalse(out["tables"]["items"])
        self.assertTrue(out["rejected"]["items"])

    def test_missing_cell_retained_as_reject(self):
        observations = source()
        observations[1]["text"] = "AB-12345-01 service | 2"
        out = mapper.map_candidates(observations, definition())
        self.assertEqual(out["rejected"]["items"][0]["source"], observations[1])
        self.assertFalse(out["tables"]["items"])

    def test_extra_literal_pipe_never_recombined(self):
        observations = source()
        observations[1]["text"] = "AB-12345-01 service | extra | 2 | 30,00"
        out = mapper.map_candidates(observations, definition())
        self.assertEqual(out["rejected"]["items"][0]["reason"], "PHYSICAL_PIPE_CELL_COUNT_MISMATCH")

    def test_empty_source_cell_never_synthesized(self):
        observations = source()
        observations[1]["text"] = "AB-12345-01 service | | 30,00"
        out = mapper.map_candidates(observations, definition())
        self.assertEqual(out["rejected"]["items"][0]["reason"], "EMPTY_SOURCE_CELL")
        self.assertEqual(len(out["tables"]["items"]), 1)
        cell = out["tables"]["items"][0]["cells"]["quantity"]
        for key in ("value", "observed", "origin", "source_fragment", "candidate_text"):
            self.assertIsNone(cell[key])
        self.assertEqual(out["tables"]["items"][0]["cells"]["amount"]["observed"], "30,00")
        self.assertEqual(out["blocks"][0]["status"], "PARTIAL")
        self.assertFalse(out["successful_literal_mapping_claims"])

    def test_partial_row_stays_shown_in_prompt_and_enters_normal_missing_review(self):
        observations = source()
        observations[1]["text"] = "AB-12345-01 service | | 30,00"
        declared = definition()
        mapped = mapper.map_candidates(observations, declared)
        merged = mapper.merge_candidates({"lines": observations}, mapped, declared, validator)
        self.assertEqual(merged["tables"]["items"][0]["quantity"]["validity"], "MISSING")
        self.assertTrue(merged["table_unparsed"]["items"])
        self.assertEqual(merged["lines"], observations)
        view = mapper.prompt_catalog_view(observations, declared, {"fields": [], "tables": []})
        self.assertEqual(view["prompt_view"]["omitted_detail_observation_ids"], [])
        self.assertFalse(mapped["independent_row_coverage"])

    def test_missing_row_key_never_synthesizes_a_partial_identity(self):
        observations = source()
        observations[1]["text"] = " | 2 | 30,00"
        out = mapper.map_candidates(observations, definition())
        self.assertFalse(out["tables"]["items"])
        self.assertEqual(out["rejected"]["items"][0]["reason"], "UNKNOWN_DECLARED_ROW_START")

    def test_duplicate_partial_row_preserves_both_rejection_reasons(self):
        observations = source()
        observations[1]["text"] = "AB-12345-01 service | | 30,00"
        observations.append(copy.deepcopy(observations[1]))
        out = mapper.map_candidates(observations, definition())
        self.assertEqual(len(out["tables"]["items"]), 2)
        self.assertEqual(
            [x["reason"] for x in out["rejected"]["items"]],
            ["EMPTY_SOURCE_CELL", "EMPTY_SOURCE_CELL", "DUPLICATE_DECLARED_ROW_KEY"],
        )
        self.assertEqual(out["blocks"][0]["rejected_rows"], 2)
        self.assertEqual(out["blocks"][0]["rejected_facts"], 3)

    def test_partial_does_not_rescue_malformed_cell_count_or_ambiguous_identity(self):
        for text, reason in [
            ("AB-12345-01 service | | 30,00 |", "PHYSICAL_PIPE_CELL_COUNT_MISMATCH"),
            ("AB-12345-01 CD-12345-02 | | 30,00", "AMBIGUOUS_MULTIPLE_DECLARED_ROW_STARTS"),
        ]:
            with self.subTest(text=text):
                observations = source()
                observations[1]["text"] = text
                out = mapper.map_candidates(observations, definition())
                self.assertFalse(out["tables"]["items"])
                self.assertEqual(out["rejected"]["items"][0]["reason"], reason)

    def test_unknown_row_start_is_not_hidden(self):
        observations = source() + [line("BADKEY unrecognized | 3 | 40,00")]
        out = mapper.map_candidates(observations, definition())
        self.assertEqual(len(out["tables"]["items"]), 1)
        self.assertEqual(out["rejected"]["items"][0]["reason"], "UNKNOWN_DECLARED_ROW_START")
        self.assertEqual(out["rejected"]["items"][0]["source"]["text"], observations[2]["text"])

    def test_duplicate_rows_retained_in_candidates_and_failure_facts(self):
        observations = source() + [copy.deepcopy(source()[1])]
        out = mapper.map_candidates(observations, definition())
        self.assertEqual(len(out["tables"]["items"]), 2)
        self.assertIn("DUPLICATE_DECLARED_ROW_KEY", out["tables"]["items"][1]["issues"])
        self.assertEqual(out["rejected"]["items"][0]["reason"], "DUPLICATE_DECLARED_ROW_KEY")

    def test_repeated_headers_are_preserved_and_not_data_rows(self):
        observations = source() + [
            copy.deepcopy(source()[0]),
            line("AB-12345-02 other | 1 | 10,00"),
        ]
        out = mapper.map_candidates(observations, definition())
        self.assertEqual(len(out["tables"]["items"]), 2)
        self.assertEqual(len(out["blocks"][0]["header_observations"]), 2)

    def test_page_continuation_with_observed_header(self):
        observations = source() + [
            line(source()[0]["text"], 2),
            line("AB-12345-02 other | 1 | 10,00", 2),
        ]
        out = mapper.map_candidates(observations, definition())
        self.assertEqual(
            [r["cells"]["amount"]["origin"]["page"] for r in out["tables"]["items"]], [1, 2]
        )

    def test_headerless_continuation_requires_declared_observed_context(self):
        observations = source() + [
            line("CONTINUED ITEMS", 2, region=[0.15, 0.07, 0.8, 0.12], label="text"),
            line("AB-12345-02 other | 1 | 10,00", 2),
        ]
        out = mapper.map_candidates(observations, definition())
        self.assertEqual(len(out["tables"]["items"]), 2)
        self.assertTrue(out["blocks"][1]["continuation_inferred"])

    def test_context_outside_declared_region_cannot_authorize_or_hide_continuation(self):
        for region in (
            [0.15, 0.8, 0.8, 0.9],
            [0.05, 0.07, 0.8, 0.12],
            [0.15, 0.07, 0.8, 0.2],
            [0.15, 0.04, 0.8, 0.12],
        ):
            with self.subTest(region=region):
                observations = source() + [
                    line("CONTINUED ITEMS", 2, region=region, label="text"),
                    line("AB-12345-02 other | 1 | 10,00", 2),
                ]
                out = mapper.map_candidates(observations, definition())
                self.assertEqual(len(out["tables"]["items"]), 1)
                self.assertEqual(out["blocks"][1]["status"], "UNASSIGNED")
                view = mapper.prompt_catalog_view(
                    observations, definition(), {"fields": [], "tables": []}
                )
                self.assertNotIn(3, view["prompt_view"]["omitted_detail_observation_ids"])
                self.assertEqual(out["blocks"][1]["raw_observations"], [observations[3]])

    def test_missing_invalid_or_non_ocr_context_geometry_cannot_authorize(self):
        for change in (
            {"region": None},
            {"region": [0.1, 0.1, 0.1, 0.12]},
            {"method": "DIGITAL"},
            {"page": 3},
        ):
            with self.subTest(change=change):
                context = line("CONTINUED ITEMS", 2, region=[0.15, 0.07, 0.8, 0.12], label="text")
                context.update(change)
                out = mapper.map_candidates(
                    source() + [context, line("AB-12345-02 other | 1 | 10,00", 2)], definition()
                )
                self.assertEqual(len(out["tables"]["items"]), 1)
                self.assertEqual(out["blocks"][1]["status"], "UNASSIGNED")

    def test_missing_or_invalid_declared_context_region_cannot_authorize(self):
        for region in (None, [0.9, 0.05, 0.1, 0.15], [0.1, 0.05, 1.1, 0.15], [0.1, 0.0, 0.9, 1.0]):
            with self.subTest(region=region):
                declared = definition()
                declared["tables"][0]["visual"]["continuation_context"]["region"] = region
                out = mapper.map_candidates(
                    source()
                    + [
                        line("CONTINUED ITEMS", 2, region=[0.15, 0.07, 0.8, 0.12], label="text"),
                        line("AB-12345-02 other | 1 | 10,00", 2),
                    ],
                    declared,
                )
                self.assertEqual(len(out["tables"]["items"]), 1)
                self.assertEqual(out["blocks"][1]["status"], "UNASSIGNED")

    def test_headerless_new_page_without_context_not_guessed(self):
        observations = source() + [line("AB-12345-02 other | 1 | 10,00", 2)]
        out = mapper.map_candidates(observations, definition())
        self.assertEqual(len(out["tables"]["items"]), 1)
        self.assertEqual(out["blocks"][1]["status"], "UNASSIGNED")

    def test_misordered_header_never_uses_declared_positions(self):
        observations = source()
        observations[0]["text"] = "Item description | Amount | Quantity"
        out = mapper.map_candidates(observations, definition())
        self.assertFalse(out["tables"]["items"])
        self.assertEqual(out["blocks"][0]["status"], "UNASSIGNED")

    def test_conflicting_header_inside_known_block_rejects_whole_block(self):
        observations = source() + [line("Item description | Amount | Quantity")]
        out = mapper.map_candidates(observations, definition())
        self.assertFalse(out["tables"]["items"])
        self.assertEqual(out["blocks"][0]["reason"], "MISORDERED_OR_CONFLICTING_HEADER")

    def test_unknown_table_block_retained_separate_from_detail(self):
        observations = source() + [line("Summary | 50,00", region=[0.5, 0.8, 0.9, 0.9])]
        out = mapper.map_candidates(observations, definition())
        self.assertEqual(out["blocks"][1]["raw_observations"][0], observations[-1])
        self.assertEqual(out["blocks"][1]["status"], "UNASSIGNED")

    def test_no_geometry_is_invented_for_missing_box(self):
        observations = source()
        observations[1].pop("region")
        out = mapper.map_candidates(observations, definition())
        self.assertFalse(out["tables"]["items"])
        self.assertIn("INVALID_SOURCE_ORIGIN", [b.get("reason") for b in out["blocks"]])

    def test_nan_box_rejected(self):
        observations = source()
        observations[1]["region"] = [0.1, 0.2, float("nan"), 0.8]
        with self.assertRaises(ValueError):
            mapper.map_candidates(observations, definition())

    def test_preheader_data_is_not_reordered(self):
        out = mapper.map_candidates(list(reversed(source())), definition())
        self.assertFalse(out["tables"]["items"])
        self.assertEqual(out["rejected"]["items"][0]["reason"], "ROW_PRECEDES_DECLARED_HEADER")

    def test_ambiguous_identical_declared_table_headers_never_choose(self):
        declared = definition()
        other = copy.deepcopy(declared["tables"][0])
        other["name"] = "other"
        declared["tables"].append(other)
        out = mapper.map_candidates(source(), declared)
        self.assertFalse(out["tables"]["items"])
        self.assertEqual(out["blocks"][0]["reason"], "AMBIGUOUS_TABLE_HEADER")

    def test_no_existing_valid_data_overwrite_and_no_old_reject_clear(self):
        existing = {
            "tables": {"items": [{"description": {"value": "KEEP", "validity": "VALID"}}]},
            "fields": {"invoice": {"value": "KEEPHEADER"}},
            "table_unparsed": {"items": [{"reason": "EXISTING_GEOMETRY_REJECTION"}]},
            "engine": {"independent_row_coverage": False},
            "lines": source(),
        }
        before = copy.deepcopy(existing)
        merged = mapper.merge_candidates(
            existing, mapper.map_candidates(source(), definition()), definition(), validator
        )
        self.assertEqual(merged["tables"], before["tables"])
        self.assertEqual(merged["fields"], before["fields"])
        self.assertEqual(merged["engine"], before["engine"])
        self.assertEqual(merged["table_unparsed"]["items"][0], before["table_unparsed"]["items"][0])
        self.assertTrue(merged["semantic_conflicts"])
        self.assertEqual(existing, before)

    def test_empty_table_populated_only_with_injected_validator(self):
        payload = {"tables": {"items": []}, "table_unparsed": {"items": []}, "lines": source()}
        merged = mapper.merge_candidates(
            payload, mapper.map_candidates(source(), definition()), definition(), validator
        )
        self.assertEqual(merged["tables"]["items"][0]["amount"]["value"], "30.00")
        self.assertFalse(merged["tables"]["items"][0]["amount"]["reviewed"])
        self.assertNotIn("quality", merged)

    def test_no_independent_coverage_or_approval_from_arithmetic_or_matching(self):
        out = mapper.map_candidates(source(), definition())
        self.assertFalse(out["independent_row_coverage"])
        self.assertFalse(out["spatial_completeness_certified"])
        self.assertFalse(out["automatic_approval_measured"])
        self.assertFalse(out["scalar_validity_measured"])

    def test_generic_declared_column_counts_use_synthetic_sources_only(self):
        for count in (3, 7, 8):
            with self.subTest(columns=count):
                declared = definition()
                table = declared["tables"][0]
                for index in range(3, count):
                    table["fields"].append({"name": f"column_{index}", "logical_type": "STRING"})
                    table["visual"]["header_labels"].append(f"Column {index}")
                values = ["AB-12345-01\\nSynthetic source", "2", "30,00"] + ["SYNTHETIC"] * (
                    count - 3
                )
                observations = [
                    line(" | ".join(table["visual"]["header_labels"])),
                    line(" | ".join(values)),
                ]
                out = mapper.map_candidates(observations, declared)
                self.assertEqual(
                    set(out["tables"]["items"][0]["cells"]),
                    {field["name"] for field in table["fields"]},
                )

    def test_nonzero_declared_row_key_column(self):
        declared = definition()
        table = declared["tables"][0]
        table["visual"]["row_start"]["column"] = 1
        table["visual"]["multiline_column"] = 1
        table["fields"][0]["logical_type"] = "DECIMAL"
        table["fields"][1]["logical_type"] = "STRING"
        observations = [
            line("Item description | Quantity | Amount"),
            line("2 | AB-12345-01\\nSynthetic source | 10,00"),
        ]
        self.assertEqual(len(mapper.map_candidates(observations, declared)["tables"]["items"]), 1)

    def test_undeclared_row_grammar_supported_by_no_guess(self):
        declared = definition()
        declared["tables"][0]["visual"].pop("row_start")
        out = mapper.map_candidates(source(), declared)
        self.assertEqual(out["unsupported_tables"], ["items"])
        self.assertFalse(out["tables"]["items"])

    def test_header_percent_decoration_recorded_not_silently_renamed(self):
        declared = definition()
        declared["tables"][0]["fields"][-1]["logical_type"] = "STRING"
        declared["invoice_controls"] = {"table": "items", "tax_percent": "amount"}
        observations = source()
        observations[0]["text"] = "Item description | Quantity | Amount %"
        out = mapper.map_candidates(observations, declared)
        self.assertEqual(
            out["blocks"][0]["header_observations"][0]["match_kinds"][-1],
            "DECLARED_LABEL_WITH_OBSERVED_PERCENT_SUFFIX",
        )
        self.assertEqual(out["blocks"][0]["raw_observations"][0]["text"], observations[0]["text"])

    def test_undeclared_percent_suffix_cannot_change_decimal_header_unit(self):
        observations = source()
        observations[0]["text"] = "Item description | Quantity | Amount %"
        out = mapper.map_candidates(observations, definition())
        self.assertFalse(out["tables"]["items"])
        self.assertEqual(out["blocks"][0]["status"], "UNASSIGNED")

    def test_continuation_false_rejects_new_page_even_with_same_header(self):
        declared = definition()
        declared["tables"][0]["visual"]["continue_pages"] = False
        observations = source() + [
            line(source()[0]["text"], 2),
            line("AB-12345-02 other | 1 | 10,00", 2),
        ]
        out = mapper.map_candidates(observations, declared)
        self.assertEqual(len(out["tables"]["items"]), 1)
        self.assertEqual(out["blocks"][1]["status"], "UNASSIGNED")

    def test_header_before_declared_start_page_not_adopted(self):
        declared = definition()
        declared["tables"][0]["visual"]["page"] = 2
        out = mapper.map_candidates(source(), declared)
        self.assertFalse(out["tables"]["items"])
        self.assertEqual(out["blocks"][0]["status"], "UNASSIGNED")

    def test_full_payload_source_lines_and_unrecognized_footer_survive_merge(self):
        observations = source() + [line("Printed footer text", label="footer")]
        payload = {"lines": observations, "tables": {"items": []}, "table_unparsed": {"items": []}}
        merged = mapper.merge_candidates(
            payload, mapper.map_candidates(observations, definition()), definition(), validator
        )
        self.assertEqual(merged["lines"], observations)
        self.assertEqual(merged["lines"][-1]["block_label"], "footer")

    def test_mutated_candidate_origin_never_adopted(self):
        observations = source()
        declared = definition()
        mapped = mapper.map_candidates(observations, declared)
        mapped["tables"]["items"][0]["cells"]["amount"]["origin"]["region"] = [0.4, 0.4, 0.5, 0.5]
        with self.assertRaisesRegex(ValueError, "SOURCE_OR_CONFIGURATION_CHANGED"):
            mapper.merge_candidates({"lines": observations}, mapped, declared, validator)

    def test_mutated_candidate_text_or_fragment_never_adopted(self):
        for key, value in [("observed", "999"), ("candidate_text", "999")]:
            with self.subTest(key=key):
                observations = source()
                declared = definition()
                mapped = mapper.map_candidates(observations, declared)
                mapped["tables"]["items"][0]["cells"]["amount"][key] = value
                with self.assertRaisesRegex(ValueError, "SOURCE_OR_CONFIGURATION_CHANGED"):
                    mapper.merge_candidates({"lines": observations}, mapped, declared, validator)

    def test_definition_or_observation_changed_after_mapping_is_held(self):
        observations = source()
        declared = definition()
        mapped = mapper.map_candidates(observations, declared)
        observations[1]["text"] = observations[1]["text"].replace("30,00", "900,00")
        with self.assertRaisesRegex(ValueError, "SOURCE_OR_CONFIGURATION_CHANGED"):
            mapper.merge_candidates({"lines": observations}, mapped, declared, validator)

    def test_two_declared_row_starts_inside_one_cell_cannot_collapse_rows(self):
        observations = source()
        observations[1]["text"] = "AB-12345-01\\nCD-12345-02 second row | 2 | 30,00"
        out = mapper.map_candidates(observations, definition())
        self.assertFalse(out["tables"]["items"])
        self.assertEqual(
            out["rejected"]["items"][0]["reason"], "AMBIGUOUS_MULTIPLE_DECLARED_ROW_STARTS"
        )
        self.assertFalse(out["claimed_detail_observation_ids"])

    def test_observed_declared_stop_in_same_block_prevents_later_detail(self):
        declared = definition()
        declared["tables"][0]["visual"]["stop_label"] = "END ITEMS"
        observations = source() + [line("END ITEMS"), line("AB-12345-02 later | 5 | 60,00")]
        out = mapper.map_candidates(observations, declared)
        self.assertEqual(len(out["tables"]["items"]), 1)
        self.assertEqual(
            [item["reason"] for item in out["rejected"]["items"]],
            ["AT_OR_AFTER_DECLARED_PAGE_STOP", "AT_OR_AFTER_DECLARED_PAGE_STOP"],
        )
        self.assertFalse(out["claimed_detail_observation_ids"])

    def test_observed_external_stop_before_new_block_is_respected(self):
        declared = definition()
        declared["tables"][0]["visual"]["stop_label_aliases"] = ["END ITEMS"]
        observations = [line("END ITEMS", region=[0.1, 0.1, 0.9, 0.15], label="text"), *source()]
        out = mapper.map_candidates(observations, declared)
        self.assertFalse(out["tables"]["items"])
        self.assertTrue(out["rejected"]["items"])

    def test_unassigned_plausible_detail_enters_authoritative_unparsed(self):
        observations = source()
        observations[0]["text"] = "Unknown item | Unknown quantity | Unknown amount"
        declared = definition()
        mapped = mapper.map_candidates(observations, declared)
        merged = mapper.merge_candidates({"lines": observations}, mapped, declared, validator)
        self.assertFalse(merged["tables"].get("items"))
        self.assertTrue(merged["table_unparsed"]["items"])
        self.assertEqual(merged["table_unparsed"]["items"][-1]["text"], observations[-1]["text"])

    def test_invalid_origin_plausible_detail_is_review_fact_without_new_box(self):
        observations = source()
        observations[1].pop("region")
        mapped = mapper.map_candidates(observations, definition())
        reject = mapped["rejected"]["items"][0]
        self.assertNotIn("region", reject)
        self.assertEqual(reject["text"], observations[1]["text"])
        self.assertIn("INVALID_SOURCE_ORIGIN", reject["reason"])

    def test_existing_dictionary_order_cannot_hide_field_disagreement(self):
        observations = source()
        declared = definition()
        mapped = mapper.map_candidates(observations, declared)
        row = {
            "amount": {"value": "AB-12345-01 Printed service"},
            "quantity": {"value": "2"},
            "description": {"value": "30.00"},
        }
        merged = mapper.merge_candidates(
            {"lines": observations, "tables": {"items": [row]}}, mapped, declared, validator
        )
        self.assertEqual(merged["tables"]["items"], [row])
        self.assertIn("TABLE:items", merged["semantic_conflicts"])

    def test_success_claims_cover_exact_ids_and_never_unknown_or_partial(self):
        observations = source() + [
            line("Summary | 80,00", region=[0.1, 0.8, 0.9, 0.9]),
            line("Footer", label="footer"),
        ]
        out = mapper.map_candidates(observations, definition())
        self.assertEqual(out["claimed_detail_observation_ids"], [0, 1])
        self.assertEqual(out["unknown_or_partial_table_observation_ids"], [2])
        self.assertEqual(len(out["observation_classification"]), len(observations))
        self.assertFalse(out["successful_literal_mapping_claims"][0]["prompt_omission_approved"])
        self.assertEqual(out["blocks"][1]["raw_observations"][0], observations[2])

    def test_invalid_scalar_is_retained_after_standard_validation(self):
        observations = source()
        observations[1]["text"] = "AB-12345-01 source | 2 | NOT_A_NUMBER"
        declared = definition()
        out = mapper.map_candidates(observations, declared)

        def scalar_validation(text, field):
            return (None, "INVALID") if text == "NOT_A_NUMBER" else validator(text, field)

        merged = mapper.merge_candidates({"lines": observations}, out, declared, scalar_validation)
        self.assertEqual(merged["tables"]["items"][0]["amount"]["validity"], "INVALID")
        self.assertEqual(merged["tables"]["items"][0]["amount"]["observed"], "NOT_A_NUMBER")

    def test_prompt_view_omits_only_complete_nontarget_detail_global_ids(self):
        observations = [
            line("Unresolved printed header", label="text"),
            *source(),
            line("Summary | 80,00", region=[0.1, 0.8, 0.9, 0.9]),
            line("Unresolved footer", label="footer"),
        ]
        result = mapper.prompt_catalog_view(
            observations, definition(), {"fields": [], "tables": []}
        )
        full, view = result["full_catalog"], result["prompt_view"]
        self.assertEqual(full, mapper.FRAGMENTS.build_catalog(observations))
        self.assertEqual(view["full_catalog_sha256"], full["sha256"])
        self.assertEqual(view["omitted_detail_observation_ids"], [1, 2])
        self.assertEqual({item["evidence_id"] for item in view["fragments"]}, {0, 3, 4})
        self.assertEqual(view["unknown_or_partial_table_observation_ids_retained"], [3])
        self.assertNotEqual(
            [item["fragment_id"] for item in view["fragments"]], list(range(len(view["fragments"])))
        )
        for item in view["fragments"]:
            self.assertEqual(item, full["fragments"][item["fragment_id"]])

    def test_target_table_keeps_all_fragments_for_qwen(self):
        result = mapper.prompt_catalog_view(
            source(), definition(), {"fields": [], "tables": ["items"]}
        )
        self.assertEqual(result["prompt_view"]["fragments"], result["full_catalog"]["fragments"])
        self.assertFalse(result["prompt_view"]["omitted_detail_observation_ids"])

    def test_partial_block_never_omitted_even_if_table_not_target(self):
        observations = source() + [line("UNKNOWN row | 3 | 50,00")]
        result = mapper.prompt_catalog_view(
            observations, definition(), {"fields": [], "tables": []}
        )
        self.assertEqual(result["prompt_view"]["fragments"], result["full_catalog"]["fragments"])
        self.assertFalse(result["prompt_view"]["omitted_detail_observation_ids"])

    def test_header_only_block_never_claimed_resolved(self):
        observations = [source()[0]]
        result = mapper.prompt_catalog_view(
            observations, definition(), {"fields": [], "tables": []}
        )
        self.assertFalse(result["prompt_view"]["omitted_detail_observation_ids"])

    def test_prompt_invalid_or_unknown_target_declaration_rejected(self):
        for target in [
            {"fields": [], "tables": ["invented"]},
            {"fields": [], "tables": ["items", "items"]},
            {"fields": ["fake_header"], "tables": []},
            {"tables": []},
        ]:
            with self.subTest(target=target), self.assertRaises(ValueError):
                mapper.prompt_catalog_view(source(), definition(), target)

    def test_large_synthetic_table_keeps_full_source_catalog_and_compact_view(self):
        observations = [line("Printed header awaiting interpretation", label="text"), source()[0]]
        observations.extend(
            line(f"AB-{index:05d}-01 printed row source | 2 | 30,00") for index in range(100)
        )
        observations.append(line("Summary | 80,00", region=[0.1, 0.8, 0.9, 0.9]))
        result = mapper.prompt_catalog_view(
            observations, definition(), {"fields": [], "tables": []}
        )
        full, view = result["full_catalog"], result["prompt_view"]
        self.assertEqual(len(view["omitted_detail_observation_ids"]), 101)
        self.assertGreater(len(full["fragments"]), 500)
        self.assertLess(len(view["fragments"]), 20)
        self.assertEqual(len(observations), 103)
        self.assertEqual(
            full["observations_sha256"],
            mapper.FRAGMENTS.build_catalog(observations)["observations_sha256"],
        )


class BoundariesTests(unittest.TestCase):
    def test_raw_blocks_bind_to_confined_observations_without_replacing_evidence(self):
        raw = source()
        cleaned = copy.deepcopy(raw)
        for observed in cleaned:
            observed.pop("block_label")
            observed["words"] = [{"text": observed["text"], "region": observed["region"]}]
        mapped = mapper.map_candidates(raw, definition())
        result = mapper.merge_candidates(
            {"lines": cleaned}, mapped, definition(), validator, observations=raw
        )
        self.assertEqual(result["lines"], cleaned)
        self.assertEqual(len(result["tables"]["items"]), 1)
        self.assertFalse(result["deterministic_pipe_candidates"]["independent_row_coverage"])

    def test_raw_blocks_cannot_bypass_confined_text_page_or_region(self):
        for key, value in [("text", "invented"), ("page", 2), ("region", [0.2, 0.3, 0.8, 0.9])]:
            with self.subTest(key=key):
                raw = source()
                mapped = mapper.map_candidates(raw, definition())
                cleaned = copy.deepcopy(raw)
                cleaned[1][key] = value
                with self.assertRaisesRegex(ValueError, "UNVALIDATED_OBSERVATION"):
                    mapper.merge_candidates(
                        {"lines": cleaned}, mapped, definition(), validator, observations=raw
                    )

    def test_empty_observations_cannot_evade_finite_catalog_work_limit(self):
        with self.assertRaisesRegex(ValueError, "LLM_FRAGMENT_LIMIT"):
            mapper.map_candidates(
                [line("", label="text")] * (mapper.MAX_OBSERVATIONS + 1), definition()
            )


if __name__ == "__main__":
    unittest.main()
