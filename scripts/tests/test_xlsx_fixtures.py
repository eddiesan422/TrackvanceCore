"""The volume oracle must detect altered values, not only row counts."""
import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest
from openpyxl import load_workbook

SCRIPT = Path(__file__).resolve().parents[1] / "xlsx_fixtures.py"
spec = importlib.util.spec_from_file_location("xlsx_fixtures", SCRIPT)
fixtures = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixtures
spec.loader.exec_module(fixtures)


@pytest.mark.parametrize("strings", ["inline", "shared"])
def test_fixture_independent_oracle_and_official_reader(tmp_path, strings):
    result = fixtures.generate(tmp_path, 103, strings=strings)
    assert result["oracle"]["status"] == "PASS"
    assert result["oracle"]["null_count"] == 1
    assert result["oracle"]["empty_count"] == 2
    workbook = load_workbook(result["path"], read_only=True, data_only=False)
    try:
        sheet = workbook["Datos"]
        assert sheet.calculate_dimension() == "A1:A1"
        sheet.reset_dimensions()
        rows = sheet.iter_rows(values_only=True)
        for _ in range(fixtures.HEADER_ROW):
            header = next(rows)
        assert tuple(header) == fixtures.COLUMNS
        first = next(rows)
        assert first[0] == "000000000001"
        assert first[1] == 2.00000001
        assert first[5].date().isoformat() == "2023-01-02"
        assert first[6] == "=A5+1"
        assert json.loads(Path(result["path"]).with_suffix(".json").read_text(encoding="utf-8"))["rows"] == 103
    finally:
        workbook.close()


def test_oracle_detects_changed_business_value_and_same_count(tmp_path):
    result = fixtures.generate(tmp_path, 3)
    source, changed = Path(result["path"]), tmp_path / "changed.xlsx"
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(changed, "x") as output:
        for info in original.infolist():
            content = original.read(info.filename)
            if info.filename == "xl/worksheets/sheet2.xml":
                content = content.replace(b"000000000003", b"999999999999")
            output.writestr(info, content)
    with pytest.raises(AssertionError, match="Oracle mismatch"):
        fixtures.verify(changed, result)
