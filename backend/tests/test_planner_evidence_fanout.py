import pytest

from trackvance import spark_engine
from trackvance.planner import ExecutionPlanner, WorkloadInput
from trackvance.processing import ProcessingError


def test_intake_fanout_is_budgeted_before_polars_materialization(tmp_path):
    planner = ExecutionPlanner(storage=tmp_path, spark_available=True)
    inputs = [WorkloadInput(200000, 1, 10000, 36)]
    config = {"rules": [{"type": "allowed_values", "column": "id",
                        "parameters": {"values": ["allowed"]},
                        "when": {"column": "id", "operator": "not_null"}} for _ in range(14)]}
    automatic = planner.plan("intake", inputs, config, free_bytes=1024**3)
    assert automatic["engine"] == "PYSPARK" and automatic["allowed"]
    assert automatic["estimated_polars_evidence_bytes"] > automatic["resource_budget"]["memory_soft_bytes"]
    explicit = planner.plan("intake", inputs, config, free_bytes=1024**3, requested_engine="POLARS")
    assert explicit["rejection_code"] == "RESOURCE_MEMORY_INSUFFICIENT"
    assert explicit["engine"] == "POLARS" and not explicit["allowed"]
    disabled = planner.plan("intake", inputs, {"rules": [{**rule, "enabled": False} for rule in config["rules"]]},
                            free_bytes=1024**3)
    assert disabled["engine"] == "POLARS" and disabled["estimated_polars_evidence_bytes"] == 0


def test_recon_comparison_details_and_wide_conditions_are_budgeted(tmp_path):
    planner = ExecutionPlanner(storage=tmp_path, spark_available=True)
    inputs = [WorkloadInput(50000, 2, 10000, 1000), WorkloadInput(50000, 2, 10000, 1000)]
    simple = {"comparison_rules": [{"type": "exact_compare", "source_column": "a", "target_column": "a"}]}
    complex_config = {"comparison_rules": simple["comparison_rules"] * 12}
    smaller = planner.plan("recon", inputs, simple, free_bytes=1024**3)
    larger = planner.plan("recon", inputs, complex_config, free_bytes=1024**3)
    assert larger["estimated_polars_evidence_bytes"] > smaller["estimated_polars_evidence_bytes"]
    assert larger["engine"] == "PYSPARK" and larger["allowed"]
    assert planner.plan("recon", inputs, complex_config, free_bytes=1024**3,
                        requested_engine="POLARS")["rejection_code"] == "RESOURCE_MEMORY_INSUFFICIENT"
    wider = {"rules": [{"type": "regex", "column": "a", "parameters": {"pattern": "a" * 10000}}]}
    narrower = {"rules": [{"type": "regex", "column": "a", "parameters": {"pattern": "a"}}]}
    assert planner.plan("intake", inputs[:1], wider, free_bytes=1024**3)["estimated_polars_evidence_bytes"] > (
        planner.plan("intake", inputs[:1], narrower, free_bytes=1024**3)["estimated_polars_evidence_bytes"])


def test_spark_recon_batch_accounts_for_complete_comparison_output(monkeypatch):
    calls = []

    def kernel(rows, *_args):
        calls.append([item[0] for item in rows])
        yield "result", len(rows)

    monkeypatch.setattr(spark_engine, "_recon_batch", kernel)
    config = {"comparison_rules": [{"type": "exact_compare", "source_column": "a", "target_column": "a"}] * 100}
    records = [((key,), (side, number, {"a": "value"}))
               for number, key in enumerate(("a", "b", "c")) for side in (0, 1)]
    result = list(spark_engine._recon_partition(iter(records), ("a",), ("a",), config, 100,
                                              batch_rows=2048, batch_bytes=200000))
    assert result == [("result", 2)] * 3
    assert calls == [[("a",), ("a",)], [("b",), ("b",)], [("c",), ("c",)]]
    with pytest.raises(ProcessingError, match="RESOURCE_RECON_GROUP_LIMIT"):
        list(spark_engine._recon_partition(iter(records), ("a",), ("a",), config, 100,
                                          max_group_bytes=100000))
