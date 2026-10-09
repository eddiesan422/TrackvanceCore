"""Independent client readers reject partial exports and compare every typed row."""
import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend/src"))
SPEC = importlib.util.spec_from_file_location("reports_download_085", Path(__file__).with_name("reports_download_085.py"))
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


@pytest.mark.parametrize("zip64_reader_fixture", [False, True])
def test_full_independent_csv_xml_and_reader_oracles_preserve_exact_values(tmp_path, monkeypatch, zip64_reader_fixture):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("TRACKVANCE_STORAGE_DIR", str(tmp_path / "storage"))
    from trackvance.report_exports import csv_stream, xlsx_stream

    expected_rows = list(runner.expected_rows(15, 3))
    expected = runner.population_hash(iter(expected_rows))
    columns = [{"name": name} for name in runner.HEADERS]
    csv_path, xlsx_path = tmp_path / "complete.csv", tmp_path / "complete.xlsx.incomplete"
    csv_path.write_bytes(b"".join(csv_stream(columns, iter([expected_rows]))))
    xlsx_path.write_bytes(b"".join(xlsx_stream(columns, iter([expected_rows]))))
    if zip64_reader_fixture:
        # Reader coverage of ZIP64 is independent from the bounded ZIP32
        # streaming product. A seekable client fixture has exact local sizes.
        with zipfile.ZipFile(xlsx_path) as source:
            members = {name: source.read(name) for name in source.namelist()}
        with zipfile.ZipFile(xlsx_path, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for name, content in members.items():
                with target.open(name, "w", force_zip64=name.endswith("sheet1.xml")) as output:
                    output.write(content)
    assert runner.population_hash(runner.csv_population(csv_path)) == expected
    assert runner.population_hash(runner.xlsx_population(xlsx_path)) == expected
    assert runner.population_hash(runner.openpyxl_population(xlsx_path)) == expected
    result = runner.validate_package(xlsx_path)
    assert result["status"] == "PASS" and result["expanded_bytes"] > result["serialized_zip_bytes"]
    assert result["zip64_present"] is zip64_reader_fixture
    assert expected_rows[0][0] == "000000000001"
    assert [row[8] for row in expected_rows[:5]] == [None, "", r"\N", "'literal", " "]


def test_partial_zip_is_never_certified(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("TRACKVANCE_STORAGE_DIR", str(tmp_path / "storage"))
    from trackvance.report_exports import xlsx_stream

    path = tmp_path / "partial.xlsx"
    content = b"".join(xlsx_stream([{"name": "id"}], iter([[["001"]]])))
    path.write_bytes(content[:-50])
    with pytest.raises(zipfile.BadZipFile):
        runner.validate_package(path)
