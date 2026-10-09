import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.ci.common import (
    FUNCTIONAL_MODULES,
    EvidenceError,
    load_manifest,
    profile_manifest,
)
from scripts.ci.select_suites import base_from_event, select

SHA = 'a' * 40
FINGERPRINT = {'algorithm': 'GIT_TRACKED_EXECUTABLE_TREE_V1', 'sha256': 'b' * 64, 'tracked_entries': 10}
INHERITANCE = {'schema_version': 1, 'kind': 'GITHUB_FUNCTIONAL_INHERITANCE', 'source_sha': SHA,
               'executable_fingerprint': FINGERPRINT, 'origin': {'run_id': 1}}


@pytest.mark.parametrize('path', ['backend/src/trackvance/api.py', 'backend/uv.lock', 'frontend/package.json',
    'frontend/src/routes.tsx', 'scripts/tests/corrections_cycle.py', 'scripts/ci/scenarios.json', 'deploy/docker/compose.yml',
    '.github/workflows/ci.yml', 'unknown/new.file', '../README.md', '/README.md', 'C:/README.md',
    'backend/tests/test_permissions.py', 'docs/development/permission-matrix.md',
    'docs/specification/permission_contract_0.8.0.json', '.gitignore', 'AGENTS.md',
    'backend/migrations/README.md', 'backend/tests/assets/input.md', 'docs/operations/docker.md',
    '.github/actions/dependencies/README.md'])
def test_uncertain_runtime_contract_and_harness_changes_select_complete_functional(path):
    result = select('auto', [path], source_sha=SHA)
    assert result['mode'] == result['profile'] == 'functional'
    assert result['groups'] == load_manifest()['profiles']['functional']['groups']
    assert not result['certifies_final'] and not result['deep_eligible']


@pytest.mark.parametrize('files,extra', [(None, {}), ([], {}), (['README.md'], {'diff_uncertain': True})])
def test_missing_or_empty_uncertain_diff_never_skips_product_tests(files, extra):
    assert select('auto', files, source_sha=SHA, **extra)['mode'] == 'functional'


@pytest.mark.parametrize('path', ['README.md', 'CHANGELOG.md', 'backend/API_CONTRACT.md', 'docs/README.md',
    'docs/specification/README.md', 'LICENSE-or-proprietary-notice.txt'])
def test_documentation_only_changes_have_no_product_groups_or_matrix(path):
    result = select('auto', [path], source_sha=SHA, verified_inheritance=INHERITANCE)
    assert result['mode'] == 'docs' and result['scope'] == 'DOCUMENTATION_ONLY'
    assert result['groups'] == [] and result['matrix'] == {'include': []}
    assert not result['certifies_final'] and not result['functional_eligible']
    assert result['functional_inheritance'] == INHERITANCE


@pytest.mark.parametrize('path', ['README.md', 'CHANGELOG.md', 'backend/API_CONTRACT.md'])
def test_documentation_delta_alone_never_excuses_unapproved_inherited_executable(path):
    result = select('auto', [path], source_sha=SHA)
    assert result['mode'] == 'functional' and result['functional_inheritance'] is None
    assert 'unverified-executable-inheritance' in result['reason']


def test_mixed_changes_and_explicit_functional_dispatch_cannot_become_docs_only():
    assert select('auto', ['README.md', 'backend/src/api.py'], source_sha=SHA)['mode'] == 'functional'
    assert select('functional', ['README.md'], source_sha=SHA)['mode'] == 'functional'
    assert select('auto', ['README.md'], source_sha=SHA, commit_message='Fix docs [ci functional]')['mode'] == 'functional'


def test_functional_has_explicit_executable_evidence_for_every_required_module():
    manifest = load_manifest()
    functional = profile_manifest(manifest, 'functional')
    assert len(functional['groups']) == 11
    scenarios = {s['id'] for g in functional['groups'] for s in g['scenarios']}
    assert len(scenarios) == 30
    coverage = manifest['profiles']['functional']['module_coverage']
    assert set(coverage) == FUNCTIONAL_MODULES
    assert all(set(value['scenario_ids']) <= scenarios for value in coverage.values())
    assert not any(g['id'].startswith(('spark-', 'async-volume-')) for g in functional['groups'])


def test_deep_preserves_all_historical_large_populations_xlsx_and_recovery():
    manifest = profile_manifest(load_manifest(), 'deep')
    groups = manifest['groups']
    assert len(groups) == 19
    assert len({j for g in groups for j in g['covers_legacy_jobs']}) == 16
    scenarios = [s for g in groups for s in g['scenarios']]
    assert len(scenarios) == len({s['id'] for s in scenarios}) == 66
    acquisitions = next(g for g in groups if g['id'] == 'corrections-acquisition')['scenarios']
    assert {(s.get('rows'), s.get('variant')) for s in acquisitions if s['validator'] == 'xlsx-acquisition'} == {
        (rows, variant) for rows in (100000, 100001, 400000, 1000000) for variant in ('inline', 'shared')}
    catalog = next(g for g in groups if g['id'] == 'catalog-reports')['scenarios']
    assert {s.get('rows') for s in catalog if s['validator'] == 'catalog-population'} == {120, 400000, 1000000}
    assert {s.get('mode') for s in catalog if s['validator'] == 'catalog-recovery'} == {'native', 'legacy', 'legacy080'}
    assert [s for s in catalog if s['id'] == 'catalog-populated080-upgrade085'] == [
        {'id': 'catalog-populated080-upgrade085', 'validator': 'catalog-recovery', 'mode': 'legacy080'}]
    assert [s for s in catalog if s['id'] == 'catalog-selective-operational-cleanup085'] == [
        {'id': 'catalog-selective-operational-cleanup085', 'validator': 'catalog-cleanup'}]
    assert {s.get('tier_mib') for s in scenarios if 'tier_mib' in s} == {100, 500, 1024}
    assert len(select('deep', [], source_sha=SHA)['matrix']['include']) == 17


@pytest.mark.parametrize('change,code', [
    (lambda m: m.pop('profiles'), 'MISSING_EXPLICIT_PROFILES'),
    (lambda m: m['profiles']['functional']['groups'].append('backend'), 'INVALID_PROFILE_GROUPS'),
    (lambda m: m['profiles']['functional']['module_coverage'].pop('reports'), 'INCOMPLETE_FUNCTIONAL_MODULE_COVERAGE'),
    (lambda m: m['profiles']['functional']['module_coverage']['reports'].update(scenario_ids=['catalog-reports-1000000']), 'INVALID_FUNCTIONAL_MODULE_SCENARIOS')])
def test_manifest_cannot_claim_unexecuted_or_incomplete_functional_coverage(tmp_path, change, code):
    manifest = copy.deepcopy(load_manifest())
    change(manifest)
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(EvidenceError, match=code):
        load_manifest(path)


def test_cold_selection_never_reuses_a_previous_pass():
    result = select('functional', [], source_sha=SHA, cache_mode='cold')
    assert result['cache_mode'] == 'cold' and result['source_sha'] == SHA
    assert 'PASS' not in json.dumps(result)


def test_matrix_selects_browser_dependencies_only_where_browser_executes():
    result = select('functional', [], source_sha=SHA)
    matrix = {row['group']: row for row in result['matrix']['include']}
    assert len(matrix) == 9
    assert matrix['connections-functional']['browser'] is True
    assert matrix['backup-basic']['browser'] is False
    assert matrix['corrections-functional']['browser'] is False
    assert matrix['catalog-reports-functional']['python'] is True
    assert all(row['python'] is False for group, row in matrix.items() if group != 'catalog-reports-functional')


@pytest.mark.parametrize('payload,expected', [
    ({'action': 'synchronize', 'before': 'b' * 40, 'pull_request': {'base': {'sha': 'c' * 40}}}, 'b' * 40),
    ({'action': 'opened', 'pull_request': {'base': {'sha': 'c' * 40}}}, 'c' * 40),
    ({'before': 'b' * 40}, 'b' * 40),
    ({'before': '0' * 40}, ''),
    ({'before': '0' * 40, 'pull_request': {'base': {'sha': 'c' * 40}}}, 'c' * 40),
    ({'before': '../README.md', 'pull_request': {'base': None}}, ''),
    ({}, '')])
def test_event_delta_uses_previous_head_and_uncertain_events_require_functional(payload, expected):
    assert base_from_event(payload) == expected


def test_selector_cli_uses_event_delta_for_documentation_followup(tmp_path, monkeypatch):
    from scripts.ci import select_suites
    previous, target_base = 'b' * 40, 'c' * 40
    event = tmp_path / 'event.json'
    event.write_text(json.dumps({'action': 'synchronize', 'before': previous,
                                'pull_request': {'base': {'sha': target_base}}}), encoding='utf-8')
    output = tmp_path / 'selection.json'
    monkeypatch.setattr(sys, 'argv', ['select_suites.py', '--head', SHA, '--event-path', str(event), '--output', str(output)])
    def changed(base, head):
        assert base == previous and head == SHA
        return ['README.md']
    monkeypatch.setattr(select_suites, 'changed_from_git', changed)
    monkeypatch.setattr(select_suites, 'executable_fingerprint', lambda _: FINGERPRINT)
    assert select_suites.main() == 0
    assert json.loads(output.read_text())['scope'] == 'FUNCTIONAL'
