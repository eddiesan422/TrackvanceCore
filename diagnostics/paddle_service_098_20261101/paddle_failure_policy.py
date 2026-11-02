"""Evidence-aware admission policy for the isolated Paddle diagnostic harness.

This module performs no inference, I/O, process control, or operational mutation.
An HTTP status or an error-code substring is not causal evidence. The caller
retains full trace bytes privately and supplies their locator/hash only when the
trace actually establishes the selected domain.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import re


class Category(str, Enum):
    SUCCESS = "SUCCESS"
    INPUT_CONFIGURATION = "INPUT_CONFIGURATION"
    GATEWAY_INTERNAL = "GATEWAY_INTERNAL"
    RUNTIME_INFRASTRUCTURE = "RUNTIME_INFRASTRUCTURE"
    PADDLE_INTERNAL = "PADDLE_INTERNAL"
    TIMEOUT = "TIMEOUT"
    CANCELLATION = "CANCELLATION"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CauseEvidence:
    """A caller-accredited causal trace, not a claim inferred by this policy."""

    domain: str
    stage: str
    exception_type: str
    locator: str
    sha256: str

    def __post_init__(self) -> None:
        domains = {
            Category.INPUT_CONFIGURATION.value,
            Category.GATEWAY_INTERNAL.value,
            Category.RUNTIME_INFRASTRUCTURE.value,
            Category.PADDLE_INTERNAL.value,
        }
        if self.domain not in domains:
            raise ValueError("CAUSE_DOMAIN_NOT_SUPPORTED")
        if not all(isinstance(x, str) and x.strip() for x in (
            self.stage, self.exception_type, self.locator
        )):
            raise ValueError("CAUSE_TRACE_METADATA_REQUIRED")
        if not isinstance(self.sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("CAUSE_TRACE_SHA256_REQUIRED")


@dataclass(frozen=True)
class Observation:
    """One completed controller observation; closure is a separate safety fact.

    resources_released must be literal True to admit another GPU load. False,
    None and non-boolean truthy substitutes cannot prove effective closure.
    Evidence is optional; without it, generic error codes remain UNKNOWN.
    """

    status: str
    resources_released: bool | None
    failure_code: str | None = None
    http_status: int | None = None
    paddle_attempt: bool = True
    evidence: CauseEvidence | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, str) or not self.status.strip():
            raise ValueError("TERMINAL_STATUS_REQUIRED")
        if self.resources_released is not None and type(self.resources_released) is not bool:
            raise ValueError("CLOSURE_MUST_BE_BOOLEAN_OR_UNKNOWN")
        if type(self.paddle_attempt) is not bool:
            raise ValueError("PADDLE_ATTEMPT_MUST_BE_BOOLEAN")
        if self.http_status is not None and (type(self.http_status) is not int or not 100 <= self.http_status <= 599):
            raise ValueError("HTTP_STATUS_INVALID")
        if self.evidence is not None and not isinstance(self.evidence, CauseEvidence):
            raise ValueError("CAUSE_EVIDENCE_MUST_BE_STRUCTURED")


@dataclass(frozen=True)
class Classification:
    category: Category
    cause_demonstrated: bool
    reason: str
    evidence: CauseEvidence | None = None


def classify(observation: Observation) -> Classification:
    """Classify observed termination; never infer OOM/CUDA from status or text."""

    status = observation.status.upper()
    if status == "COMPLETE":
        if observation.evidence is not None or observation.failure_code or (
            observation.http_status is not None and observation.http_status >= 400
        ):
            return Classification(Category.UNKNOWN, False, "CONFLICTING_SUCCESS_AND_FAILURE_METADATA")
        return Classification(Category.SUCCESS, False, "TERMINAL_COMPLETE_NOT_BUSINESS_APPROVAL")
    if status == "TIMEOUT":
        return Classification(Category.TIMEOUT, False, "WORK_DEADLINE_OBSERVED_INTERNAL_CAUSE_NOT_INFERRED", observation.evidence)
    if status in {"CANCELLED", "CANCELED"}:
        return Classification(Category.CANCELLATION, False, "CONTROLLED_CANCELLATION_NOT_INFRASTRUCTURE", observation.evidence)
    if observation.evidence is not None:
        return Classification(
            Category(observation.evidence.domain), True,
            "CALLER_ACCREDITED_CAUSAL_TRACE", observation.evidence,
        )
    return Classification(Category.UNKNOWN, False, "NO_CAUSAL_TRACE_HTTP_STATUS_AND_CODE_ARE_INSUFFICIENT")


@dataclass(frozen=True)
class Decision:
    classification: Classification
    allow_next_gpu_load: bool
    suspend_reason: str | None
    consecutive_infrastructure_failures: int
    consecutive_unknown_paddle_failures: int
    unrecovered_execution_failures: int

    def to_dict(self) -> dict:
        return asdict(self)


class AdmissionGuard:
    """Sticky diagnostic suspension; never closes processes or erases evidence.

    Three successive infrastructure failures or three unknown Paddle failures
    suspend. A further conservative ceiling also suspends after three unrecovered
    execution failures of mixed/known domains, without relabelling their cause.
    Input/configuration failures and controlled cancellations neither increment
    nor erase service-failure counters: neither demonstrates service recovery.
    Only a COMPLETE Paddle attempt with confirmed closure resets those counters.
    No observation can reopen a suspended guard; a new authorized campaign must
    create a new guard instead of refunding/retrying dispatched attempts.
    """

    def __init__(self) -> None:
        self._infrastructure = 0
        self._unknown = 0
        self._unrecovered = 0
        self._suspend_reason: str | None = None

    @property
    def can_admit(self) -> bool:
        return self._suspend_reason is None

    @property
    def suspend_reason(self) -> str | None:
        return self._suspend_reason

    def observe(self, observation: Observation) -> Decision:
        if not self.can_admit:
            raise RuntimeError("ADMISSION_ALREADY_SUSPENDED_NEW_LOAD_FORBIDDEN")
        outcome = classify(observation)
        # Closure proof has precedence over success, error domain and counters.
        if observation.resources_released is not True:
            self._suspend_reason = "GPU_CLOSURE_UNCONFIRMED"
        elif observation.paddle_attempt and outcome.category == Category.SUCCESS:
            self._infrastructure = self._unknown = self._unrecovered = 0
        elif outcome.category not in {
            Category.SUCCESS, Category.INPUT_CONFIGURATION, Category.CANCELLATION
        }:
            self._unrecovered += 1
            self._infrastructure = self._infrastructure + 1 if outcome.category == Category.RUNTIME_INFRASTRUCTURE else 0
            self._unknown = self._unknown + 1 if outcome.category == Category.UNKNOWN and observation.paddle_attempt else 0
            if self._infrastructure >= 3:
                self._suspend_reason = "THREE_ACCREDITED_INFRASTRUCTURE_FAILURES"
            elif self._unknown >= 3:
                self._suspend_reason = "THREE_UNKNOWN_PADDLE_FAILURES_CAUSE_UNPROVEN"
            elif self._unrecovered >= 3:
                self._suspend_reason = "THREE_UNRECOVERED_EXECUTION_FAILURES_CAUSES_RETAINED"
        return Decision(
            outcome, self.can_admit, self._suspend_reason,
            self._infrastructure, self._unknown, self._unrecovered,
        )
