"""ID1: staggered observed columns cannot split or mix product identities."""
import copy

import pytest
from test_document_ocr_visibility_097 import source_line, table_definition

from trackvance.document_engine import extract
from trackvance.document_template_builder_rules import box, table_header_anchors, visual_lines


def word_line(text, x, y, page=1):
    line = source_line([(x, text)], y, page)
    for word in line['words']:
        word['region'][3] = y + .006
    line['region'] = box(line['words'])
    return line


def header(slope=0, page=1):
    return [word_line(text, x, .2 + slope * x, page)
            for text, x in [('Description', .05), ('Qty', .5), ('Amount', .8)]]


def staggered_source(slope, page=1):
    lines = header(slope, page)
    for text, qty, amount, y in [('QA-A first service', '2', '17.50', .3),
                                ('QA-B second service', '3', '21.75', .4)]:
        lines.extend([word_line(text, .05, y + slope * .05, page),
            word_line(qty, .5, y + .012 + slope * .5, page),
            word_line(amount, .8, y + .012 + slope * .8, page)])
    return lines


@pytest.mark.parametrize('slope', [.014, -.014])
def test_observed_staggered_columns_retain_complete_rows_and_original_origins(slope):
    source = staggered_source(slope)
    original = copy.deepcopy(source)
    definition = table_definition()
    labels = definition['tables'][0]['visual']['header_labels']
    assert any(table_header_anchors(line, labels) for line in visual_lines(source, .012))
    result = extract(source, definition)
    assert [[row[key]['value'] for key in ['description', 'quantity', 'amount']]
            for row in result['tables']['detail']] == [
                ['QA-A first service', '2', '17.50'], ['QA-B second service', '3', '21.75']]
    assert result['table_unparsed']['detail'] == []
    for index, row in enumerate(result['tables']['detail']):
        for offset, key in enumerate(['description', 'quantity', 'amount']):
            assert row[key]['origin']['region'] == source[3 + index * 3 + offset]['region']
    assert source == original


def test_configured_row_tolerance_is_not_a_silent_limit_on_complete_staggered_columns():
    definition = table_definition()
    definition['tables'][0]['visual']['row_tolerance'] = .04
    result = extract(staggered_source(.014), definition)
    assert len(result['tables']['detail']) == 2
    assert not result['table_unparsed']['detail']


def test_dense_interleaved_neighbor_columns_do_not_create_a_wrong_product():
    source = header() + [word_line('QA-A', .05, .285), word_line('QA-B', .05, .293),
        word_line('2', .5, .3), word_line('17.50', .8, .310),
        word_line('3', .5, .308), word_line('21.75', .8, .318)]
    result = extract(source, table_definition())
    actual = {tuple(row[key]['value'] for key in ['description', 'quantity', 'amount'])
              for row in result['tables']['detail']}
    assert actual <= {('QA-A', '2', '17.50'), ('QA-B', '3', '21.75')}
    assert result['table_unparsed']['detail']
    all_visible = ' '.join([cell['observed'] or '' for row in result['tables']['detail']
        for cell in row.values()] + [region['text'] for region in result['table_unparsed']['detail']])
    assert all(text in all_visible for text in ['QA-A', 'QA-B', '2', '3', '17.50', '21.75'])


def test_header_angle_opposite_to_dense_body_cannot_mix_neighboring_scalar_values():
    definition = table_definition()
    definition['tables'][0]['visual'].update(column_bounds=[0, .4, .7, 1], row_tolerance=.018)
    source = [word_line(text, x, .2 + .014 * x) for text, x in
              [('Description', .1), ('Qty', .5), ('Amount', .85)]]
    for name, quantity, amount, y in [('QA-0', '2', '17.50', .3), ('QA-1', '3', '21.75', .309)]:
        source.extend([word_line(name, .1, y - .014 * .1),
            word_line(quantity, .5, y - .014 * .5 + .012),
            word_line(amount, .85, y - .014 * .85 + .012)])
    for line in source:
        line['words'][0]['region'][2] = line['words'][0]['region'][0] + .02
        line['region'] = box(line['words'])
    result = extract(source, definition)
    assert result['tables']['detail'] == []
    visible = ' '.join(region['text'] for region in result['table_unparsed']['detail'])
    assert all(value in visible for value in ['QA-0', 'QA-1', '2', '3', '17.50', '21.75'])


def test_horizontal_rows_and_duplicate_legitimate_values_remain_separate():
    source = header() + [source_line([(.05, 'QA-A'), (.5, '2'), (.8, '17.50')], .3, 1),
        source_line([(.05, 'QA-B'), (.5, '2'), (.8, '17.50')], .4, 1)]
    result = extract(source, table_definition())
    assert len(result['tables']['detail']) == 2
    assert [row['description']['value'] for row in result['tables']['detail']] == ['QA-A', 'QA-B']
    assert not result['table_unparsed']['detail']


def test_page_slopes_are_observed_independently_and_not_carried_as_a_global_setting():
    source = staggered_source(.014, 1) + staggered_source(-.014, 2)
    result = extract(source, table_definition())
    assert len(result['tables']['detail']) == 4
    assert not result['table_unparsed']['detail']
    assert [row['quantity']['origin']['page'] for row in result['tables']['detail']] == [1, 1, 2, 2]
