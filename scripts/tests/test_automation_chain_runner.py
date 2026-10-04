"""Do not let the negative SQL integration helper escape its disposable scope."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('automation_chain_runner', Path(__file__).with_name('automation_cycle.py'))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.mark.parametrize('project,database', [
    ('trackvance-certification', 'postgresql://tv_v070_test:synthetic@isolated/tv_v070_test'),
    ('trackvance-v070-test-negatives-0123456789ab', 'postgresql://production:synthetic@isolated/business'),
])
def test_chain_matrix_rejects_production_scope_before_sql_or_api(monkeypatch, project, database):
    monkeypatch.setenv('TRACKVANCE_CERTIFICATION_PROJECT', project)
    monkeypatch.setenv('DATABASE_URL', database)
    with pytest.raises(RuntimeError):
        runner.certify_chain_decisions(None, None, None, source_id='', dataset_id='',
            draft={}, organization_id='', actor_id='', nonce='0123456789ab')


def test_chain_matrix_requires_an_exact_owned_sql_identifier(monkeypatch):
    monkeypatch.setenv('TRACKVANCE_CERTIFICATION_PROJECT', 'trackvance-v070-test-negatives-0123456789ab')
    monkeypatch.setenv('DATABASE_URL', 'postgresql://tv_v070_test:synthetic@isolated/tv_v070_test')
    with pytest.raises(RuntimeError, match='identidad sintética propia'):
        runner.certify_chain_decisions(None, None, None, source_id='', dataset_id='',
            draft={}, organization_id='', actor_id='', nonce='foreign;DROP TABLE business')
