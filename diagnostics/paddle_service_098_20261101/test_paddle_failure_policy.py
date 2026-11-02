"""CPU-only failure/admission controls; no Paddle, Docker, model or network use."""
import unittest

from paddle_failure_policy import (
    AdmissionGuard, Category, CauseEvidence, Observation, classify,
)


def causal(domain, exception_type="SyntheticRecordedException", stage="synthetic_test_stage"):
    return CauseEvidence(domain.value, stage, exception_type, "synthetic/trace.json", "a" * 64)


def observed(status="ERROR", *, closed=True, code=None, http=None, evidence=None, attempted=True):
    return Observation(status, closed, code, http, attempted, evidence)


class PaddleFailurePolicyTests(unittest.TestCase):
    def test_three_accredited_infrastructure_failures_suspend(self):
        guard = AdmissionGuard()
        for index in range(3):
            decision = guard.observe(observed(evidence=causal(Category.RUNTIME_INFRASTRUCTURE)))
            self.assertEqual(decision.allow_next_gpu_load, index < 2)
            self.assertEqual(decision.classification.category, Category.RUNTIME_INFRASTRUCTURE)
            self.assertTrue(decision.classification.cause_demonstrated)
        self.assertEqual(decision.suspend_reason, "THREE_ACCREDITED_INFRASTRUCTURE_FAILURES")

    def test_three_opaque_422_failures_suspend_without_false_infrastructure_cause(self):
        guard = AdmissionGuard()
        for index in range(3):
            decision = guard.observe(observed(code="PADDLE_HTTP_422:INFERENCE_FAILED", http=422))
            self.assertEqual(decision.allow_next_gpu_load, index < 2)
            self.assertEqual(decision.classification.category, Category.UNKNOWN)
            self.assertFalse(decision.classification.cause_demonstrated)
            self.assertEqual(decision.consecutive_infrastructure_failures, 0)
        self.assertEqual(decision.suspend_reason, "THREE_UNKNOWN_PADDLE_FAILURES_CAUSE_UNPROVEN")

    def test_input_validation_is_not_infrastructure_or_service_recovery(self):
        guard = AdmissionGuard()
        guard.observe(observed(code="PADDLE_HTTP_422", http=422))
        decision = guard.observe(observed(
            code="INVALID_REQUEST", http=422, attempted=False,
            evidence=causal(Category.INPUT_CONFIGURATION, "RequestValidationError", "request_validation"),
        ))
        self.assertEqual(decision.classification.category, Category.INPUT_CONFIGURATION)
        self.assertTrue(decision.allow_next_gpu_load)
        self.assertEqual(decision.consecutive_infrastructure_failures, 0)
        self.assertEqual(decision.consecutive_unknown_paddle_failures, 1)

    def test_timeout_with_confirmed_closure_is_separate_and_can_admit_next(self):
        guard = AdmissionGuard()
        decision = guard.observe(observed("TIMEOUT", code="INFERENCE_TIMEOUT"))
        self.assertEqual(decision.classification.category, Category.TIMEOUT)
        self.assertFalse(decision.classification.cause_demonstrated)
        self.assertTrue(decision.allow_next_gpu_load)
        self.assertEqual(decision.consecutive_infrastructure_failures, 0)

    def test_controlled_cancellation_with_closure_is_not_infrastructure(self):
        decision = AdmissionGuard().observe(observed("CANCELLED", code="REQUEST_CANCELLED"))
        self.assertEqual(decision.classification.category, Category.CANCELLATION)
        self.assertTrue(decision.allow_next_gpu_load)
        self.assertEqual(decision.unrecovered_execution_failures, 0)

    def test_success_with_confirmed_closure_resets_failures(self):
        guard = AdmissionGuard()
        guard.observe(observed(code="PADDLE_HTTP_422", http=422))
        guard.observe(observed(code="PADDLE_HTTP_422", http=422))
        decision = guard.observe(observed("COMPLETE", http=200))
        self.assertEqual(decision.classification.category, Category.SUCCESS)
        self.assertTrue(decision.allow_next_gpu_load)
        self.assertEqual(decision.consecutive_unknown_paddle_failures, 0)
        self.assertEqual(decision.unrecovered_execution_failures, 0)
        self.assertTrue(guard.observe(observed(code="PADDLE_FAILURE")).allow_next_gpu_load)

    def test_unconfirmed_closure_immediately_blocks_and_cannot_reopen(self):
        for closed in (False, None):
            with self.subTest(closed=closed):
                guard = AdmissionGuard()
                decision = guard.observe(observed("COMPLETE", closed=closed))
                self.assertFalse(decision.allow_next_gpu_load)
                self.assertEqual(decision.suspend_reason, "GPU_CLOSURE_UNCONFIRMED")
                with self.assertRaisesRegex(RuntimeError, "ADMISSION_ALREADY_SUSPENDED"):
                    guard.observe(observed("COMPLETE"))

    def test_same_422_can_have_different_proven_causes(self):
        for category in (Category.INPUT_CONFIGURATION, Category.RUNTIME_INFRASTRUCTURE, Category.PADDLE_INTERNAL):
            with self.subTest(category=category):
                c = classify(observed(http=422, code="INFERENCE_FAILED", evidence=causal(category)))
                self.assertEqual(c.category, category)
                self.assertTrue(c.cause_demonstrated)
        self.assertEqual(classify(observed(http=422, code="INFERENCE_FAILED")).category, Category.UNKNOWN)

    def test_paddle_internal_requires_accredited_trace_not_event_label(self):
        self.assertEqual(classify(observed(code="PADDLE_FAILURE")).category, Category.UNKNOWN)
        c = classify(observed(code="PADDLE_FAILURE", evidence=causal(Category.PADDLE_INTERNAL)))
        self.assertEqual(c.category, Category.PADDLE_INTERNAL)
        self.assertTrue(c.cause_demonstrated)

    def test_gateway_causal_trace_remains_distinct_and_suspends_conservatively(self):
        guard = AdmissionGuard()
        for index in range(3):
            decision = guard.observe(observed(
                http=422, code="INFERENCE_FAILED",
                evidence=causal(Category.GATEWAY_INTERNAL, "JSONDecodeError", "gateway_serialization"),
            ))
            self.assertEqual(decision.classification.category, Category.GATEWAY_INTERNAL)
            self.assertTrue(decision.classification.cause_demonstrated)
            self.assertEqual(decision.consecutive_infrastructure_failures, 0)
            self.assertEqual(decision.consecutive_unknown_paddle_failures, 0)
            self.assertEqual(decision.allow_next_gpu_load, index < 2)
        self.assertEqual(decision.suspend_reason, "THREE_UNRECOVERED_EXECUTION_FAILURES_CAUSES_RETAINED")

    def test_failure_words_alone_do_not_prove_oom_cuda_or_infrastructure(self):
        for code in ("OOM", "CUDA error", "GPU_RUNTIME_UNAVAILABLE", "ConnectionRefusedError", "deadlock"):
            with self.subTest(code=code):
                c = classify(observed(code=code))
                self.assertEqual(c.category, Category.UNKNOWN)
                self.assertFalse(c.cause_demonstrated)

    def test_mixed_unrecovered_execution_failures_suspend_without_relabelling(self):
        guard = AdmissionGuard()
        cases = [observed(evidence=causal(Category.RUNTIME_INFRASTRUCTURE)), observed("TIMEOUT"), observed(code="PADDLE_FAILURE")]
        categories = []
        for item in cases:
            decision = guard.observe(item)
            categories.append(decision.classification.category)
        self.assertEqual(categories, [Category.RUNTIME_INFRASTRUCTURE, Category.TIMEOUT, Category.UNKNOWN])
        self.assertFalse(decision.allow_next_gpu_load)
        self.assertEqual(decision.suspend_reason, "THREE_UNRECOVERED_EXECUTION_FAILURES_CAUSES_RETAINED")

    def test_success_conflicting_with_http_failure_does_not_reset_failures(self):
        guard = AdmissionGuard()
        guard.observe(observed(code="PADDLE_HTTP_422"))
        decision = guard.observe(observed("COMPLETE", http=422))
        self.assertEqual(decision.classification.category, Category.UNKNOWN)
        self.assertEqual(decision.consecutive_unknown_paddle_failures, 2)

    def test_causal_metadata_requires_trace_identity(self):
        for kwargs in ({"sha256": ""}, {"sha256": "a" * 63}, {"locator": ""}, {"stage": ""}, {"domain": "OOM"}):
            with self.subTest(kwargs=kwargs):
                values = dict(domain=Category.PADDLE_INTERNAL.value, stage="inference", exception_type="RuntimeError", locator="private/trace", sha256="a" * 64)
                values.update(kwargs)
                with self.assertRaises(ValueError):
                    CauseEvidence(**values)

    def test_truthy_closure_substitutes_are_rejected(self):
        for bad in (1, "true", [], {}):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "CLOSURE_MUST_BE_BOOLEAN"):
                    observed("COMPLETE", closed=bad)


if __name__ == "__main__":
    unittest.main()
