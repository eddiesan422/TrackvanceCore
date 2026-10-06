import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.ci.common import EvidenceError, load_manifest
from scripts.ci.validators import (
    integrity,
    native_fixture_restore,
    validate_content,
    validate_junit,
)


@pytest.mark.parametrize("spec", [s for g in load_manifest()["groups"] for s in g["scenarios"]], ids=lambda s: s["id"])
def test_no_scenario_can_be_certified_by_a_standalone_pass_flag(spec):
    with pytest.raises((EvidenceError, KeyError, TypeError)):
        validate_content(spec, {"status": "PASS"})


def test_full_population_hash_cannot_hide_sample_or_physical_number_loss():
    digest = "a" * 64
    with pytest.raises(EvidenceError, match="INCOMPLETE_POPULATION"):
        integrity({"rows": 100, "canonical_rows_sha256": digest}, 1000000, digest)
    with pytest.raises(EvidenceError, match="POPULATION_ORACLE_MISMATCH"):
        integrity({"rows": 1000000, "canonical_rows_sha256": "b" * 64}, 1000000, digest)
    with pytest.raises(EvidenceError, match="PHYSICAL_NUMBERING_MISMATCH"):
        integrity({"rows": 1000000, "canonical_rows_sha256": digest}, 1000000, digest, numbering=True)


def test_skips_only_allowed_for_exact_dedicated_spark_opt_in_reason():
    name = "test_real_spark_job_group_cancellation_stops_computation"
    case = {"id": "tests.test_spark_engine." + name, "classname": "tests.test_spark_engine", "name": name,
            "failure": False, "error": False, "skip": "NOT_RUN_OPT_IN: dedicated suite requires TRACKVANCE_SPARK_TESTS=1"}
    validate_junit({"cases": [case]}, allow_spark_opt_in=True)
    with pytest.raises(EvidenceError, match="UNEXPECTED_TEST_SKIP"):
        validate_junit({"cases": [case]})
    case["skip"] = "strace unavailable"
    with pytest.raises(EvidenceError, match="UNEXPECTED_TEST_SKIP"):
        validate_junit({"cases": [case]}, allow_spark_opt_in=True)


def test_new_skips_cannot_impersonate_the_known_opt_in_spark_reason():
    case = {"id": "tests.test_spark_engine.new_case", "classname": "tests.test_spark_engine", "name": "new_case",
            "failure": False, "error": False, "skip": "NOT_RUN_OPT_IN: dedicated suite requires TRACKVANCE_SPARK_TESTS=1"}
    with pytest.raises(EvidenceError, match="UNEXPECTED_TEST_SKIP"):
        validate_junit({"cases": [case]}, allow_spark_opt_in=True)


def test_browser_requires_actual_cases_and_zero_skip_or_flakiness():
    from scripts.ci.validators import browser
    with pytest.raises(EvidenceError, match="BROWSER_EMPTY"):
        browser({"status": "PASS", "expected": 0, "skipped": 0, "unexpected": 0, "flaky": 0})
    with pytest.raises(EvidenceError, match="BROWSER_SKIP_FAILURE_OR_FLAKY"):
        browser({"status": "PASS", "expected": 1, "skipped": 0, "unexpected": 0, "flaky": 1})


@pytest.mark.parametrize('damage', ['missing-core', 'duplicate', 'missing-exclusion', 'unmapped-new-spec'])
def test_general_browser_cannot_omit_applicable_specs_or_hide_unknown_opt_ins(tmp_path, monkeypatch, damage):
    from scripts.ci import validators

    spec = next(s for g in load_manifest()['groups'] for s in g['scenarios'] if s['validator'] == 'compose-browser')
    optional = {
        'automation.spec.ts': ('TV_AUTOMATION_E2E', 'async-volume-100'),
        'connections.spec.ts': ('TV_CONNECTIONS_E2E', 'connections'),
        'corrections-volume.spec.ts': ('TV_CORRECTIONS_E2E', 'corrections-browser'),
        'delivery.spec.ts': ('TV_DELIVERY_E2E', 'delivery'),
        'demo-access-clean.spec.ts': ('TV_EXPECT_CLEAN_DEMO', 'compose-critical'),
        'identity-sso.spec.ts': ('TV_IDENTITY_SSO_E2E', 'identity-sso'),
        'roadmap-source-cycle.spec.ts': ('TV_CONNECTIONS_E2E', 'connections'),
        'volume.spec.ts': ('TV_VOLUME_E2E', 'async-volume-100'),
    }
    value = {'selected_spec_files': list(spec['browser_required_specs']),
             'excluded_opt_in_specs': [{'file': 'tests-e2e/' + name, 'required_flag': flag, 'covered_by_group': group}
                                      for name, (flag, group) in sorted(optional.items())]}
    validators.browser_selection(value, spec)
    if damage == 'missing-core':
        value['selected_spec_files'].pop()
    elif damage == 'duplicate':
        value['selected_spec_files'].append(value['selected_spec_files'][0])
    elif damage == 'missing-exclusion':
        value['excluded_opt_in_specs'].pop()
    else:
        fixture = tmp_path / 'frontend/tests-e2e'
        fixture.mkdir(parents=True)
        for name in value['selected_spec_files'] + [row['file'] for row in value['excluded_opt_in_specs']]:
            (fixture / Path(name).name).write_text('')
        (fixture / 'new.spec.ts').write_text('')
        monkeypatch.setattr(validators, 'ROOT', tmp_path)
    with pytest.raises(EvidenceError, match='BROWSER_'):
        validators.browser_selection(value, spec)


@pytest.mark.parametrize("field,value,code", [
    ("revision", "0016_acquisition_diagnostics", "NATIVE_FIXTURE_RESTORE_SCHEMA"),
    ("tables", 42, "NATIVE_FIXTURE_RESTORE_SCHEMA"),
    ("automatic_processes_started", True, "NATIVE_FIXTURE_RESTORE_CONSUMERS_OR_MAIN"),
    ("restored_state_sha256", "b" * 64, "NATIVE_FIXTURE_RESTORE_HASH_MISMATCH"),
    ("verified_delivery_secrets", 0, "NATIVE_FIXTURE_RESTORE_ARTIFACT_OR_KEY_FAMILY"),
    ("cleanup_error_type", "RuntimeError", "NATIVE_FIXTURE_RESTORE_ERROR"),
])
def test_native_fixture_restore_requires_schema_exact_hashes_key_families_and_stopped_consumers(field, value, code):
    receipt = {"status": "PASS", "main_inventory": "UNCHANGED", "automatic_processes_started": False,
               "restore": "STOPPED_VERIFIED", "revision": "0017_catalog_reports", "state_schema_version": 8,
               "tables": 55, "multipart_integrity": "PASS", "historical_bytes_and_hashes": "PASS",
               "exact_state_comparison": "PASS", "source_state_sha256": "a" * 64,
               "restored_state_sha256": "a" * 64, "manifest_sha256": "c" * 64,
               "verified_artifacts": 1, "verified_source_secrets": 1, "verified_delivery_secrets": 1}
    native_fixture_restore(receipt)
    receipt[field] = value
    with pytest.raises(EvidenceError, match=code):
        native_fixture_restore(receipt)
