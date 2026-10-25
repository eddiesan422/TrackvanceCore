"""Retry recovery never replaces preceding source readings or approval evidence."""
import copy
import unittest
from types import SimpleNamespace

from trackvance import document_local_inference as local
from trackvance.document_quality import quality_report, validate_candidate_contract

retain = local.retain_disagreements

def cell(value, **changes):
    result = {'value': value, 'observed': value, 'validity': 'VALID' if value is not None else 'MISSING', 'origin': {'page': 1, 'region': [0.2, 0.3, 0.4, 0.35], 'method': 'OCR'} if value is not None else None, 'confidence': None, 'reviewed': False}
    return {**result, **changes}

def payload(rows=(('A', None), ('B', '2'))):
    return {'fields': {}, 'tables': {'detail': [{'sku': cell(key), 'amount': cell(amount)} for key, amount in rows]}, 'semantic_conflicts': [], 'independent_row_coverage': False, 'inference_completion': {'status': 'PARTIAL', 'immutable': 'preserved'}}

def definition():
    return {'tables': [{'name': 'detail', 'fields': [{'name': 'sku', 'business_key': True}, {'name': 'amount'}]}]}

class EmptyFillTests(unittest.TestCase):

    def assert_prior_retained(self, before, after, spec=None):
        previous = copy.deepcopy(before)
        proposal = copy.deepcopy(after)
        result = retain(before, after, definition() if spec is None else spec)
        self.assertEqual([[c['value'] for c in row.values()] for row in result['tables']['detail']], [[c['value'] for c in row.values()] for row in previous['tables']['detail']])
        self.assertIn('REEXTRACTION_TABLE:detail', result['semantic_conflicts'])
        self.assertEqual(result['reextraction_table_candidates']['detail']['after'], proposal['tables']['detail'])
        self.assertEqual(before, previous)
        return result

    def test_valid_fill_preserves_prior_nonempty_cells_and_records_both_reads(self):
        before, after = (payload(), payload((('A', '1'), ('B', '2'))))
        before['tables']['detail'][1]['amount']['old_provenance'] = 'keep-exact'
        after['tables']['detail'][1]['amount']['origin']['region'] = [0.5, 0.3, 0.6, 0.35]
        old = copy.deepcopy(before)
        proposed = copy.deepcopy(after)
        result = retain(before, after, definition())
        self.assertEqual(result['tables']['detail'][0]['amount'], proposed['tables']['detail'][0]['amount'])
        self.assertEqual(result['tables']['detail'][1], old['tables']['detail'][1])
        self.assertEqual(result['semantic_conflicts'], [])
        record = result['reextraction_table_candidates']['detail']
        self.assertEqual(record['reason'], 'EMPTY_MISSING_FILLED')
        self.assertEqual(record['filled_cells'], [{'row': 0, 'field': 'amount'}])
        self.assertEqual(record['before'], old['tables']['detail'])
        self.assertEqual(record['after'], proposed['tables']['detail'])
        self.assertEqual(result['reextraction_table_history']['detail'], [record])
        self.assertEqual(before, old)
        self.assertIsNot(result['tables']['detail'][0]['amount'], proposed['tables']['detail'][0]['amount'])
        self.assertFalse(result['independent_row_coverage'])
        self.assertEqual(result['inference_completion'], old['inference_completion'])
        self.assertTrue(all(c['reviewed'] is False for row in result['tables']['detail'] for c in row.values()))

    def test_zero_and_multiple_fills_with_remaining_missing_cell(self):
        before = payload((('A', None), ('B', None), ('C', None)))
        result = retain(before, payload((('A', '0'), ('B', '2'), ('C', None))), definition())
        self.assertEqual([row['amount']['value'] for row in result['tables']['detail']], ['0', '2', None])
        self.assertEqual(result['semantic_conflicts'], [])
        self.assertEqual(len(result['reextraction_table_candidates']['detail']['filled_cells']), 2)

    def test_added_removed_reordered_or_changed_rows_still_keep_all_prior(self):
        for rows in [(('A', '1'),), (('A', '1'), ('B', '2'), ('C', '3')), (('B', '2'), ('A', '1')), (('A', '1'), ('B', '9'))]:
            with self.subTest(rows=rows):
                self.assert_prior_retained(payload(), payload(rows))

    def test_duplicate_missing_or_ambiguous_identity_never_authorizes_fill(self):
        for before, after in [(payload((('A', None), ('A', '2'))), payload((('A', '1'), ('A', '2')))), (payload(((None, None), ('B', '2'))), payload(((None, '1'), ('B', '2'))))]:
            with self.subTest(before=before):
                self.assert_prior_retained(before, after)
        for change in [{'validity': 'AMBIGUOUS'}, {'alternatives': ['A', 'C']}, {'reextraction_conflict': True}]:
            before = payload()
            before['tables']['detail'][0]['sku'].update(change)
            with self.subTest(change=change):
                self.assert_prior_retained(before, payload((('A', '1'), ('B', '2'))))

    def test_old_cell_must_be_truly_empty_not_an_observed_or_conflicted_read(self):
        for change in [{'observed': ''}, {'observed': '?'}, {'value': ''}, {'validity': 'INVALID'}, {'validity': 'VALID'}, {'origin': {}}, {'source_fragment': {}}, {'candidate_text': '?'}, {'alternatives': ['1']}, {'reextraction_conflict': True}, {'reviewed': True}, {'corrected': True}]:
            before = payload()
            before['tables']['detail'][0]['amount'].update(change)
            with self.subTest(change=change):
                self.assert_prior_retained(before, payload((('A', '1'), ('B', '2'))))

    def test_proposed_fill_must_be_valid_literal_unreviewed_remapped_evidence(self):
        for change in [{'validity': 'INVALID'}, {'validity': 'MISSING'}, {'observed': None}, {'value': None}, {'observed': ''}, {'value': ''}, {'reviewed': True}, {'corrected': True}, {'alternatives': ['1', '9']}, {'reextraction_conflict': True}, {'origin': None}, {'origin': {'page': 1, 'region': [0, 0, 2, 1], 'method': 'OCR'}}, {'origin': {'page': 1, 'region': [0, 0, 1, 1]}}]:
            after = payload((('A', '1'), ('B', '2')))
            after['tables']['detail'][0]['amount'].update(change)
            with self.subTest(change=change):
                self.assert_prior_retained(payload(), after)

    def test_observed_change_with_same_normalized_value_is_a_conflict(self):
        after = payload((('A', '1'), ('B', '2')))
        after['tables']['detail'][1]['amount']['observed'] = '2.00'
        result = self.assert_prior_retained(payload(), after)
        self.assertEqual(result['tables']['detail'][1]['amount']['validity'], 'AMBIGUOUS')
        self.assertEqual(result['tables']['detail'][1]['amount']['alternatives'], ['2', '2.00'])

    def test_existing_other_cell_ambiguity_does_not_enable_pure_fill_path(self):
        for change in [{'validity': 'AMBIGUOUS'}, {'alternatives': ['2', '9']}]:
            before, after = (payload(), payload((('A', '1'), ('B', '2'))))
            before['tables']['detail'][1]['amount'].update(change)
            after['tables']['detail'][1]['amount'].update(change)
            with self.subTest(change=change):
                self.assert_prior_retained(before, after)

    def test_no_declared_identity_or_schema_drift_can_fill(self):
        no_key = definition()
        no_key['tables'][0]['fields'][0].pop('business_key')
        for spec in [{}, no_key]:
            with self.subTest(spec=spec):
                self.assert_prior_retained(payload(), payload((('A', '1'), ('B', '2'))), spec)
        after = payload((('A', '1'), ('B', '2')))
        after['tables']['detail'][0]['extra'] = cell('extra')
        self.assert_prior_retained(payload(), after)

    def test_declared_row_start_and_composite_keys_allow_only_unique_matches(self):
        spec = definition()
        spec['tables'][0]['fields'][0].pop('business_key')
        spec['tables'][0]['visual'] = {'row_start': {'column': 0}}
        self.assertFalse(retain(payload(), payload((('A', '1'), ('B', '2'))), spec)['semantic_conflicts'])
        spec = definition()
        spec['tables'][0]['fields'].append({'name': 'site', 'business_key': True})
        before, after = (payload((('A', None), ('A', '2'))), payload((('A', '1'), ('A', '2'))))
        for target in (before, after):
            for row, site in zip(target['tables']['detail'], ['X', 'Y'], strict=True):
                row['site'] = cell(site)
        self.assertFalse(retain(before, after, spec)['semantic_conflicts'])

    def test_existing_table_conflicts_are_not_resolved_by_filling(self):
        for scope in ('before', 'after'):
            for prefix in ('TABLE:', 'REEXTRACTION_TABLE:'):
                before, after = (payload(), payload((('A', '1'), ('B', '2'))))
                (before if scope == 'before' else after)['semantic_conflicts'] = [prefix + 'detail']
                with self.subTest(scope=scope, prefix=prefix):
                    self.assert_prior_retained(before, after)

    def test_prior_history_and_unrelated_conflict_are_preserved(self):
        before = payload()
        old_history = {'before': [], 'after': [], 'reason': 'PRIOR_AUDIT'}
        before['reextraction_table_history'] = {'detail': [old_history]}
        before['semantic_conflicts'] = ['FIELD:invoice']
        result = retain(before, payload((('A', '1'), ('B', '2'))), definition())
        self.assertEqual(result['semantic_conflicts'], ['FIELD:invoice'])
        self.assertEqual(result['reextraction_table_history']['detail'][0], old_history)
        result['tables']['detail'][0]['amount']['origin']['region'][0] = 0.9
        self.assertEqual(result['reextraction_table_history']['detail'][1]['after'][0]['amount']['origin']['region'][0], 0.2)

    def test_dictionary_order_alone_does_not_add_conflict_or_history(self):
        before = payload((('A', '1'), ('B', '2')))
        after = copy.deepcopy(before)
        after['tables']['detail'] = [{'amount': row['amount'], 'sku': row['sku']} for row in after['tables']['detail']]
        result = retain(before, after, definition())
        self.assertEqual(result['semantic_conflicts'], [])
        self.assertNotIn('reextraction_table_history', result)

    def test_filled_payload_stays_unreviewed_without_independent_row_coverage(self):
        spec = definition()
        spec['fields'] = []
        for field in spec['tables'][0]['fields']:
            field.update(logical_type='STRING' if field['name'] == 'sku' else 'DECIMAL', required=True)
        result = retain(payload(), payload((('A', '1'), ('B', '2'))), spec)
        result.update(table_unparsed={'detail': []}, lines=[], pages=[1])
        engine = {'key': 'LOCAL_AI', 'route': 'PADDLE', 'independent_row_coverage': False}
        validate_candidate_contract({'payload': result, 'engine': engine}, spec)
        report = quality_report(SimpleNamespace(payload=result, engine=engine, reviewed=False), spec)
        self.assertEqual(report['status'], 'REVIEW_REQUIRED')
        self.assertFalse(report['checks']['row_coverage'])
        self.assertFalse(report['checks']['independent_extraction'])
        self.assertIn('QUALITY_ROW_COVERAGE:detail', report['issues'])
        self.assertTrue(all(c['reviewed'] is False for row in result['tables']['detail'] for c in row.values()))
