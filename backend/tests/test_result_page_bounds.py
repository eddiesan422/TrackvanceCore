import json

import polars as pl
import pytest

from trackvance.artifactstore import storage_provider
from trackvance.models import Run
from trackvance.operations_common import OperationError
from trackvance.services import iter_result_rows, result_rows


def test_result_page_checks_utf8_bytes_before_decoding_and_preserves_full_population():
    # Tiny Parquet bytes can represent a large result; compressed file size and
    # row count alone must not authorize a huge HTTP materialization.
    payload = json.dumps({"classification": "ERROR", "received_value": "界" * 1_000_000}, ensure_ascii=False)
    path = storage_provider.temporary_path(".parquet")
    pl.DataFrame({"classification": ["ERROR"] * 6, "payload": [payload] * 6}).write_parquet(path)
    run = Run(module="intake", result_path=str(path))
    with pytest.raises(OperationError) as failure:
        result_rows(run, limit=6)
    assert failure.value.code == "RESULT_PAGE_BYTE_LIMIT" and failure.value.status == 422
    page = result_rows(run, offset=2, limit=2)
    assert page["total"] == 6 and len(page["items"]) == 2
    assert page["items"][0]["received_value"] == "界" * 1_000_000
    assert sum(1 for _ in iter_result_rows(run, batch_rows=1)) == 6
