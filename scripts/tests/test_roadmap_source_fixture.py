import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("roadmap_fixture", Path(__file__).with_name("roadmap_source_fixture.py"))
assert spec and spec.loader
fixture_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture_module)


@pytest.mark.parametrize("project", ["trackvance-certification", "trackvance-ci", "unrelated", "trackvance-connections-e2e-x;evil"])
def test_source_fixture_never_writes_to_main_or_untrusted_project(project, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No Docker command should run for an unauthorized project.")
    monkeypatch.setattr(fixture_module.subprocess, "run", forbidden)
    with pytest.raises(ValueError):
        fixture_module.fixture(project, "POSTGRESQL", "correct")


def test_fixture_requires_a_single_label_scoped_engine(monkeypatch):
    monkeypatch.setattr(fixture_module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="one\ntwo\n"))
    with pytest.raises(ValueError, match="único"):
        fixture_module.fixture("trackvance-connections-e2e-test", "SQLSERVER", "setup")


def test_sql_fixture_does_not_accept_user_sql_or_interpolate_credentials(monkeypatch):
    commands = []

    def run(arguments, **kwargs):
        commands.append((arguments, kwargs))
        return SimpleNamespace(returncode=0, stdout="container-id\n")

    monkeypatch.setattr(fixture_module.subprocess, "run", run)
    fixture_module.fixture("trackvance-connections-e2e-test", "SQLSERVER", "correct")
    assert "label=com.docker.compose.project=trackvance-connections-e2e-test" in commands[0][0]
    assert commands[1][0][:4] == ["docker", "exec", "-i", "container-id"]
    assert "$MSSQL_SA_PASSWORD" in commands[1][0][-1]
    assert commands[1][1]["input"] == "UPDATE source_data.roadmap_transactions SET department = 'Bogota' WHERE record_id = '002';"
    with pytest.raises(ValueError):
        fixture_module.fixture("trackvance-connections-e2e-test", "SQLSERVER", "DROP DATABASE")
