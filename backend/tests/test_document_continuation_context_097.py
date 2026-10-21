"""Generic continuation-context closure and exact legacy serialization contracts."""
import copy
import hashlib

import pytest

from trackvance import document_engine as candidate
from trackvance import document_template_builder_schemas as candidate_schema
from trackvance.document_schemas import DocumentDefinition

LABEL = "CONTINUACIÓN DE EJEMPLO"

def line(page, y, values):
    words = [{'text': text, 'region': [x, y, x + width, y + .008]} for text, x, width in values]
    return {'page': page, 'text': ' '.join(word['text'] for word in words), 'words': words,
        'method': 'OCR', 'confidence': 90,
        'region': [min(w['region'][0] for w in words), y, max(w['region'][2] for w in words), y + .008]}


def data():
    labels = [('Description', .05, .2), ('Qty', .50, .05), ('Amount', .80, .12)]
    actual = [line(1, .20, labels), line(1, .30, [('QA-A', .05, .15), ('2', .50, .03), ('17.50', .8, .10)]),
        line(2, .03, [('Example', .05, .09), ('CAFE', .16, .13), ('Company', .50, .09), ('INV-000001', .80, .15)]),
        line(2, .06, [(text, .05 + i * .10, .08) for i, text in enumerate(LABEL.split())]),
        line(2, .20, labels), line(2, .30, [('QA-B', .05, .15), ('3', .50, .03), ('21.75', .8, .10)])]
    fields = [{'name': 'description', 'logical_type': 'STRING', 'label': 'Description'},
        {'name': 'quantity', 'logical_type': 'DECIMAL', 'label': 'Qty', 'decimal_separator': '.', 'thousands_separator': None},
        {'name': 'amount', 'logical_type': 'DECIMAL', 'label': 'Amount', 'decimal_separator': '.', 'thousands_separator': None}]
    rule = {'page': 1, 'region': [.05, .20, .95, .22], 'header_labels': ['Description', 'Qty', 'Amount'],
        'column_bounds': [0, .40, .70, 1], 'flowing': True, 'continue_pages': True,
        'row_tolerance': .018, 'multiline_column': 0, 'tolerance': .12,
        'continuation_context': {'region': [0, .01, 1, .10], 'label': LABEL}}
    return actual, rule, fields, ['Example CAFÉ Company', 'INV-000001']


def run(lines, rule, fields, values, engine=candidate):
    return engine._visual_rules.table_candidates(lines, rule, fields, values, engine.validate_value)


def tokens(regions):
    return ' '.join(region['text'] for region in regions)


def test_positive_complete_context_removes_metadata_only():
    lines, rule, fields, headers = data()
    before = copy.deepcopy(lines)
    rows, unparsed = run(lines, rule, fields, headers)
    assert [[value for value, _ in row] for row in rows] == [['QA-A', '2', '17.50'], ['QA-B', '3', '21.75']]
    assert unparsed == []
    assert lines == before


@pytest.mark.parametrize('change', ['unknown', 'valid_scalar', 'valid_zero', 'leading_zero', 'percent', 'ambiguous_partition',
    'missing_header', 'all_string', 'boundary_overlap', 'wrong_region', 'literal_incomplete', 'duplicate_label'])
def test_ambiguous_or_unknown_population_stays_visible(change):
    lines, rule, fields, headers = data()
    expected = None
    if change in {'unknown', 'leading_zero', 'percent'}:
        expected = {'unknown': 'BODY-SKU', 'leading_zero': '001', 'percent': '%'}[change]
        lines.append(line(2, .085, [(expected, .1, .08)]))
    elif change in {'valid_scalar', 'valid_zero'}:
        expected = '2' if change == 'valid_scalar' else '0'
        lines.append(line(2, .085, [(expected, .5, .03)]))
    elif change == 'ambiguous_partition':
        headers += ['Example', 'CAFÉ Company']
    elif change == 'missing_header':
        headers = []
    elif change == 'all_string':
        for field in fields:
            field['logical_type'] = 'STRING'
    elif change == 'boundary_overlap':
        expected = 'BOUNDARY'
        lines.append(line(2, .097, [('BOUNDARY', .1, .10)]))
    elif change == 'wrong_region':
        rule['continuation_context']['region'] = [0, .055, 1, .10]
    elif change == 'literal_incomplete':
        rule['continuation_context']['label'] += ' EXTRA'
    elif change == 'duplicate_label':
        lines.append(line(2, .085, [(text, .05 + i * .10, .08) for i, text in enumerate(LABEL.split())]))
    rows, unparsed = run(lines, rule, fields, headers)
    assert unparsed, change
    if expected:
        assert expected in tokens(unparsed)
    assert [[value for value, _ in row] for row in rows] == [['QA-A', '2', '17.50'], ['QA-B', '3', '21.75']]


def test_opt_in_never_excludes_first_page():
    lines, rule, fields, headers = data()
    prefix = [copy.deepcopy(entry) for entry in lines if entry['page'] == 2 and entry['region'][1] < .1]
    for entry in prefix:
        entry['page'] = 1
    rule['region'][1] = .01
    lines += prefix
    _, unparsed = run(lines, rule, fields, headers)
    assert any(region['page'] == 1 and 'CAFE' in region['text'] for region in unparsed)


@pytest.mark.parametrize('bad', [
    {'region': [0, .01, 1, .9], 'label': LABEL},
    {'region': [0, .01, 1, .1], 'label': '001'},
    {'region': [0, .01, 1, .1], 'label': '%'},
    {'region': [0, .01, 1, .1], 'label': LABEL, 'unexpected': True},
])
def test_context_schema_rejects_unbounded_or_nonliteral_or_extra_contract(bad):
    _, rule, _, _ = data()
    rule['continuation_context'] = bad
    with pytest.raises(ValueError):
        candidate_schema.VisualTableRule.model_validate(rule)


# Golden generic normalization captured from the pre-feature model.
LEGACY_VISUAL = {'page': 1, 'region': [0.05, 0.2, 0.95, 0.22], 'header_labels': ['Description', 'Qty', 'Amount'], 'column_bounds': [0.0, 0.4, 0.7, 1.0], 'stop_label': '', 'stop_label_aliases': [], 'tolerance': 0.12, 'flowing': True, 'continue_pages': True, 'multiline_column': 0, 'row_tolerance': 0.018}
LEGACY_VISUAL_JSON = '{"page":1,"region":[0.05,0.2,0.95,0.22],"header_labels":["Description","Qty","Amount"],"column_bounds":[0.0,0.4,0.7,1.0],"stop_label":"","stop_label_aliases":[],"tolerance":0.12,"flowing":true,"continue_pages":true,"multiline_column":0,"row_tolerance":0.018}'
LEGACY_DEFINITION_JSON_SHA256 = '80cf10e89818d5e8b4b39623ccfe73d217996310e348b926f809ac4e0d542c99'


@pytest.mark.parametrize('mode', ['python', 'json'])
@pytest.mark.parametrize('options', [{}, {'include': {'region', 'header_labels'}},
    {'exclude': {'flowing', 'stop_label'}}, {'exclude_none': True}, {'exclude_unset': True}, {'exclude_defaults': True}])
def test_legacy_serialization_and_filters_unchanged(mode, options):
    _, rule, _, _ = data()
    rule.pop('continuation_context')
    model = candidate_schema.VisualTableRule.model_validate(rule)
    actual = model.model_dump(mode=mode, **options)
    expected = copy.deepcopy(LEGACY_VISUAL)
    if options.get('include'):
        expected = {key: value for key, value in expected.items() if key in options['include']}
    if options.get('exclude'):
        expected = {key: value for key, value in expected.items() if key not in options['exclude']}
    if options.get('exclude_none'):
        expected = {key: value for key, value in expected.items() if value is not None}
    if options.get('exclude_unset'):
        expected = {key: value for key, value in expected.items() if key in rule}
    if options.get('exclude_defaults'):
        expected = {key: value for key, value in expected.items() if key in {'region', 'header_labels', 'column_bounds', 'flowing', 'continue_pages', 'multiline_column'}}
    assert actual == expected
    assert 'continuation_context' not in actual
    if not options:
        assert model.model_dump_json() == LEGACY_VISUAL_JSON
        assert candidate_schema.VisualTableRule.model_validate(actual).model_dump(mode=mode) == expected


def test_nested_legacy_definition_json_hash_and_roundtrip_unchanged():
    _, rule, fields, _ = data()
    rule.pop('continuation_context')
    definition = {'fields': [{'name': 'issuer', 'label': 'Issuer', 'logical_type': 'STRING'}],
        'tables': [{'name': 'items', 'fields': fields, 'visual': rule}]}
    model = DocumentDefinition.model_validate(definition)
    encoded = model.model_dump_json()
    assert hashlib.sha256(encoded.encode()).hexdigest() == LEGACY_DEFINITION_JSON_SHA256
    assert model.model_dump()['tables'][0]['visual'] == LEGACY_VISUAL
    assert DocumentDefinition.model_validate_json(encoded).model_dump_json() == encoded


def test_opt_in_model_roundtrip_and_individual_exclusion():
    _, rule, _, _ = data()
    model = candidate_schema.VisualTableRule.model_validate(rule)
    encoded = model.model_dump_json()
    assert model.model_dump(mode='json')['continuation_context'] == rule['continuation_context']
    assert candidate_schema.VisualTableRule.model_validate_json(encoded).model_dump_json() == encoded
    assert model.model_dump(exclude={'continuation_context'}) == LEGACY_VISUAL
    assert model.model_copy(update={'continuation_context': None}).model_dump() == LEGACY_VISUAL
    assert set(model.model_dump(include={'continuation_context'})) == {'continuation_context'}


@pytest.mark.parametrize('literal', ['INV-00000¹', 'INV-０００００１'])
def test_compatibility_identifier_glyphs_are_not_accent_variants(literal):
    lines, rule, fields, headers = data()
    lines[2]['words'][-1]['text'] = literal
    lines[2]['text'] = ' '.join(word['text'] for word in lines[2]['words'])
    _, unparsed = run(lines, rule, fields, headers)
    assert unparsed
    assert literal in tokens(unparsed)


@pytest.mark.parametrize('actual,wanted', [
    ('INV-000001\u0338', 'INV-000001'),
    ('INV-000001\u0301', 'INV-000001'),
    ('INV-K00001', 'INV-K00001'),
    ('INV-Ạ00001', 'INV-A00001'),
    ('INV-000001\u20e3', 'INV-000001'),
    ('INV-000001\u200b', 'INV-000001'),
    ('INV-000001\u034f', 'INV-000001'),
])
def test_only_supported_accents_on_latin_letters_may_be_ignored(actual, wanted):
    lines, rule, fields, headers = data()
    headers[-1] = wanted
    lines[2]['words'][-1]['text'] = actual
    lines[2]['text'] = ' '.join(word['text'] for word in lines[2]['words'])
    _, unparsed = run(lines, rule, fields, headers)
    assert unparsed
    assert actual in tokens(unparsed)


def test_decomposed_latin_acute_is_an_allowed_accent_variant():
    lines, rule, fields, headers = data()
    headers[0] = "Example CAFE\u0301 Company"
    _, unparsed = run(lines, rule, fields, headers)
    assert unparsed == []
