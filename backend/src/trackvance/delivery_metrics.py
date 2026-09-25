"""Additive definitions for Delivery evidence; historical evidence stays readable."""


def delivery_metric_semantics() -> dict[str, str | int]:
    """Return a fresh, serializable contract for receipt/manifest/Run consumers."""
    return {
        "version": 1,
        "rows_attempted": "SOURCE_ROWS_PREPARED",
        "rows_written": "SOURCE_ROWS_SUBMITTED_IN_COMMITTED_OPERATION",
        "rows_inserted": "ADAPTER_REPORTED_INSERT_ACTIONS_OR_NULL",
        "rows_updated": "ADAPTER_REPORTED_UPDATE_ACTIONS_OR_NULL",
        "bytes_sent": "UTF8_PREPARED_NON_NULL_VALUES_NOT_WIRE_BYTES",
        "physical_destination_rows": "NOT_MEASURED",
    }
