"""PRIVATE stdlib-only synthetic reporting checks: no OCR/model/API resources."""
import copy
import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path

import idp_benchmark
import idp_capacity
import idp_corpus

SCRIPTS = Path(__file__).resolve().parents[1]
COMPLETION = SCRIPTS.parent / "backend/src/trackvance/document_inference_completion.py"
spec = importlib.util.spec_from_file_location("private_completion", COMPLETION)
completion = importlib.util.module_from_spec(spec)
spec.loader.exec_module(completion)


class OperationError(Exception):
    def __init__(self, status=422, code="CONTROL_UNAVAILABLE"):
        self.status, self.code = status, code


def row(status, identity="synthetic"):
    return {"id": identity, "status": status, "cells": 7, "exact_cells": 7,
        "header_fields": 1, "exact_header_fields": 1, "expected_rows": 2, "actual_rows": 2,
        "exact_rows": 2, "pages": 1, "document_exact": True, "table_exact": True,
        "primary_reference_complete": True, "latency_seconds": 1, "auto_approval_eligible": True,
        "incomplete_inference": {"failed_stage": "REFINE"}, "phases": {"extraction_seconds": .8}}


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="private-reporting-fixture-")
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / "synthetic.pdf"
        self.source.write_bytes(b"synthetic bytes, never a real PDF")
        self.source_sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.clock = [0.0]
        self.budget = completion.AttemptBudget(30, clock=lambda: self.clock[0])
        self.phases = {}
        self.calls = []
        self.data = {"payload": {"fields": {}, "tables": {}}, "engine": {}}

    def provider(self, source, definition, configuration, **kwargs):
        self.calls.append(("provider", kwargs))
        self.assertIs(kwargs["budget"], self.budget)
        self.clock[0] = 10
        kwargs["check"](30)
        return copy.deepcopy(self.data)

    def control(self, source, definition, **kwargs):
        self.calls.append(("control", kwargs))
        self.assertEqual(kwargs["deadline"], 25)
        self.assertIs(kwargs["control_deadline"], True)
        self.clock[0] += 2
        kwargs["check"](90)
        return {"independent": True}

    def run_candidate(self, **overrides):
        arguments = {"provider": self.provider, "control": self.control,
            "certify": lambda *args: {"independent": True},
            "report_quality": lambda extraction, definition: {"status": "AUTO_VERIFIED"},
            "operation_error": OperationError, "checkpoint": lambda progress: None,
            "budget": self.budget, "phases": self.phases, "clock": lambda: self.clock[0]}
        arguments.update(overrides)
        return idp_corpus.run_automatic(self.source, {}, {}, self.source_sha, **arguments)

    def test_complete_uses_provider_boundary_and_same_total_budget_for_control(self):
        data, outcome = self.run_candidate()
        self.assertEqual(outcome["status"], "PROCESSED")
        self.assertTrue(outcome["auto_approval_eligible"])
        self.assertEqual(data["engine"]["deadline_budget"]["observed_attempt_seconds"], 12)
        self.assertEqual(self.phases, {"extraction_seconds": 10, "independent_validation_seconds": 2, "quality_seconds": 0})
        self.assertFalse(outcome["native_approval_measured"])
        self.assertIsNone(outcome["native_auto_approved"])
        self.assertFalse(outcome["publication_measured"])
        self.assertEqual(outcome["human_edits"], 0)

    def test_partial_stage_is_retained_and_never_eligible_even_if_quality_claims_auto(self):
        marker = {"status": "PARTIAL", "failed_stage": "REFINE", "error_code": "DOCUMENT_TIMEOUT"}
        self.data["engine"]["inference_completion"] = marker
        data, outcome = self.run_candidate()
        self.assertEqual(outcome["status"], "PARTIAL")
        self.assertEqual(outcome["failed_phase"], "REFINE")
        self.assertEqual(outcome["timeout_origin"], "OPTIONAL_STAGE")
        self.assertEqual(data["engine"]["inference_completion"], marker)
        self.assertFalse(outcome["auto_approval_eligible"])

    def test_total_deadline_is_fatal_after_candidate_and_preserves_attempted_phase_timing(self):
        captured = []
        def control(source, definition, **kwargs):
            self.clock[0] = 31
            kwargs["check"](90)
        with self.assertRaises(completion.DocumentDeadlineExpired):
            self.run_candidate(control=control, capture_candidate=lambda data: captured.append(copy.deepcopy(data)))
        self.assertEqual(len(captured), 1)
        self.assertEqual(self.phases["independent_validation_seconds"], 21)

    def test_mandatory_stage_cutoff_is_distinct_from_total_expiry(self):
        error = completion.DocumentDeadlineExpired()
        self.clock[0] = 16
        self.assertEqual(idp_corpus.deadline_failure(error, self.budget, completion.DocumentDeadlineExpired)["timeout_origin"], "MANDATORY_STAGE_DEADLINE")
        self.clock[0] = 30
        self.assertEqual(idp_corpus.deadline_failure(error, self.budget, completion.DocumentDeadlineExpired)["timeout_origin"], "TOTAL_DEADLINE")
        self.assertEqual(idp_corpus.deadline_failure(OperationError(), self.budget, completion.DocumentDeadlineExpired), {})

    def test_callback422_is_fatal_and_cannot_be_control_unavailability(self):
        error = OperationError(422, "SYNTHETIC_AUTHORITY_FAILURE")
        def checkpoint(progress):
            if progress == 90:
                raise error
        with self.assertRaises(idp_corpus.CorpusCheckpointFailure) as caught:
            self.run_candidate(checkpoint=checkpoint)
        self.assertIs(caught.exception.original, error)

    def test_ordinary_control422_remains_reviewable_and_observed(self):
        for code in ("CONTROL_UNAVAILABLE", "DOCUMENT_TIMEOUT"):
            with self.subTest(code=code):
                def unavailable(*args, _code=code, **kwargs):
                    raise OperationError(code=_code)
                data, outcome = self.run_candidate(control=unavailable,
                    report_quality=lambda extraction, definition: {"status": "REVIEW_REQUIRED"})
                self.assertEqual(outcome["status"], "PROCESSED")
                self.assertFalse(outcome["auto_approval_eligible"])
                self.assertEqual(data["engine"]["validation_failure"]["error_code"], code)
                self.assertIn("independent_validation_seconds", self.phases)

    def test_independent_integrity_contract_cancel_and_lease422_are_fatal(self):
        for code in ("DOCUMENT_CANCELLED", "DOCUMENT_LEASE_LOST", "DOCUMENT_ARTIFACT_INTEGRITY",
                     "DOCUMENT_EXTRACTION_INTEGRITY", "DOCUMENT_PROVIDER_CONTRACT", "DOCUMENT_LOCAL_INFERENCE_CONTRACT"):
            with self.subTest(code=code):
                error = OperationError(code=code)
                def fatal(*args, _error=error, **kwargs):
                    raise _error
                with self.assertRaises(OperationError) as caught:
                    self.run_candidate(control=fatal)
                self.assertIs(caught.exception, error)

    def test_changed_source_after_control_is_fatal(self):
        def control(source, definition, **kwargs):
            source.write_bytes(b"changed fixture source")
            return {}
        with self.assertRaisesRegex(ValueError, "CORPUS_SOURCE_INTEGRITY_FAILURE"):
            self.run_candidate(control=control)

    def test_all100_denominators_include_partial_scores_and_failed_zero(self):
        rows = [row("PROCESSED", "complete")]
        rows += [row("PARTIAL", "partial-" + str(index)) for index in range(98)]
        rows += [{"id": "failed", "status": "FAILED", "cells": 7, "header_fields": 1,
            "expected_rows": 2, "pages": 1, "latency_seconds": 2, "error_code": "DOCUMENT_TIMEOUT",
            "timeout_origin": "TOTAL_DEADLINE"}]
        summary = idp_corpus.summarize(rows, 10)
        self.assertEqual((summary["documents"], summary["processed"], summary["partial"], summary["failed"]), (100, 1, 98, 1))
        self.assertEqual((summary["cells"], summary["exact_cells"], summary["pages"], summary["expected_rows"]), (700, 693, 100, 200))
        self.assertEqual(summary["auto_approval_eligible"], 1)  # A forged partial eligibility flag cannot add approval.
        self.assertEqual(summary["document_exact"], 1)
        self.assertEqual(summary["partial_content_exact_documents"], 98)
        self.assertEqual(summary["global_deadline_exhausted_documents"], 1)
        self.assertEqual(summary["partial_failed_stage_counts"], {"REFINE": 98})
        self.assertEqual(summary["phase_durations"]["extraction_seconds"]["observed_documents"], 99)
        self.assertEqual(summary["human_edits"], 0)

    def test_benchmark_reports_partial_separately_from_process_exit_and_native_approval(self):
        report = {"results": [row("PROCESSED", "a"), row("PARTIAL", "b")],
            "summary": {"documents": 2, "processed": 1, "partial": 1, "failed": 0}}
        result = idp_benchmark.workload_outcomes(report, 2)
        self.assertEqual(result["completion_status"], "PARTIAL")
        self.assertFalse(result["native_approval_measured"])
        self.assertIsNone(result["native_auto_approved"])
        self.assertEqual(result["partial"], 1)
        report["summary"]["processed"] = 2
        with self.assertRaisesRegex(ValueError, "COUNTS_MISMATCH"):
            idp_benchmark.workload_outcomes(report, 2)

    def test_missing_duplicate_or_nonterminal_benchmark_row_rejects(self):
        for rows in ([row("PROCESSED")], [row("PROCESSED"), row("PROCESSED")],
                     [row("PROCESSED", "a"), row("RUNNING", "b")]):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, "DENOMINATOR_INVALID"):
                idp_benchmark.workload_outcomes({"results": rows, "summary": {}}, 2)

    def test_cli_metrics_cannot_hide_native_publication_or_human_edits(self):
        for key, value in (("human_edits", 1), ("human_corrections", 1), ("native_auto_approved", True),
                           ("native_approval_measured", True), ("publication_measured", True)):
            report = {"results": [row("PROCESSED", "a")],
                "summary": {"documents": 1, "processed": 1, "partial": 0, "failed": 0}}
            report["results"][0][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "SCOPE_OR_HUMAN_EDIT_MISMATCH"):
                idp_benchmark.workload_outcomes(report, 1)

    def test_capacity_rejects_partial_even_with_contradictory_complete_counter(self):
        report = {"summary": {"documents": 100, "processed": 100, "partial": 1, "failed": 0, "elapsed_seconds": 60}}
        with self.assertRaisesRegex(ValueError, "NO_COMPLETE_MEASURED_RUN"):
            idp_capacity.capacity([report], documents_per_user_per_day=5, window_hours=8,
                target_utilization=.6, retention_days=365, artifact_multiplier=2)


if __name__ == "__main__":
    unittest.main()
