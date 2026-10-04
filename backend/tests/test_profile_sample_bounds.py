from datetime import UTC, datetime
from decimal import Decimal

from trackvance.api import bounded_profile_sample


class Records:
    def __init__(self, rows):
        self.rows = rows
        self.visited = 0

    def head(self, limit):
        for row in self.rows[:limit]:
            self.visited += 1
            yield row


def test_presentation_sample_stops_on_encoded_utf8_budget_without_population_materialization():
    records = Records([{"text": "界" * 30}] * 100)
    sample, size, limited = bounded_profile_sample(records, byte_limit=210)
    assert len(sample) == 2 and size == 205 and limited
    assert records.visited == 3


def test_sample_accounts_for_json_escaping_and_can_omit_one_oversized_record():
    records = Records([{"text": "\u0000" * 30}])
    sample, size, limited = bounded_profile_sample(records, byte_limit=100)
    assert sample == [] and size == 2 and limited


def test_sample_preserves_native_values_and_row_limit():
    row = {"money": Decimal("0.00000000000000000001"), "when": datetime(2026, 10, 3, tzinfo=UTC)}
    records = Records([row] * 100)
    sample, size, limited = bounded_profile_sample(records)
    assert sample == [row] * 20 and size > 0 and not limited
    assert isinstance(sample[0]["money"], Decimal)
    assert records.visited == 20
