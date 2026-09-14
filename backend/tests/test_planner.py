from trackvance.planner import ExecutionPlanner, ResourceBudget, WorkloadInput


def test_resource_preflight_and_unavailable_engine(tmp_path):
    planner = ExecutionPlanner(ResourceBudget(memory_soft_bytes=10_000, temp_min_free_bytes=100), tmp_path)
    allowed = planner.plan("intake", [WorkloadInput(1, 3, 100)], {}, free_bytes=500)
    assert allowed["allowed"] and allowed["engine"] == "POLARS"
    disk = planner.plan("intake", [WorkloadInput(1, 3, 100)], {}, free_bytes=399)
    assert not disk["allowed"] and disk["rejection_code"] == "RESOURCE_DISK_INSUFFICIENT"
    large = planner.plan("recon", [WorkloadInput(1000, 16, 100_000)], {}, free_bytes=1_000_000)
    assert not large["allowed"] and large["engine"] == "PYSPARK" and large["rejection_code"] == "ENGINE_UNAVAILABLE"
