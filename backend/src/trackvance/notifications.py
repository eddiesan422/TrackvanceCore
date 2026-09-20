"""Future delivery boundary; local alerts are persisted Findings, visible in the cockpit.

No external delivery adapter is installed. A future outbox/Email/Teams/Slack/Webhook
adapter must consume these immutable references without changing DatasetSource or engines.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AlertReference:
    organization_id: str
    finding_id: str
    run_id: str
    configuration_id: str
    severity: str


class NotificationDelivery(Protocol):
    def deliver(self, alert: AlertReference, *, idempotency_key: str) -> None: ...
