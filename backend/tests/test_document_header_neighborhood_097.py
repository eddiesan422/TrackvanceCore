"""Combined physical skew and literal multiline captions; no corpus fixtures."""
import copy
import math

import pytest
from test_document_skew_rows_097 import table_definition, word_line

from trackvance.document_engine import extract
from trackvance.document_template_builder_rules import box, header_neighborhoods, visual_lines


def source_and_definition(width, height, angle, pitch=.014, extra=None):
    definition = table_definition()
    labels = ['Item description', 'Qty units', 'Line amount']
    table = definition['tables'][0]
    table['visual'].update(header_labels=labels, region=[.04, .18, .99, .25])
    for field, label in zip(table['fields'], labels, strict=True):
        field['label'] = label
    source = []
    for label, x in zip(labels, [.05, .5, .8], strict=True):
        first, last = label.split()
        source.extend([word_line(first, x + .008, .2), word_line(last, x, .2 + pitch)])
        for line in source[-2:]:
            line['words'][0]['region'][2] = line['words'][0]['region'][0] + .10
            line['words'][0]['region'][3] = line['words'][0]['region'][1] + .010
            line['region'] = box(line['words'])
    if extra:
        source.append(word_line(extra, .30, .21))
    for text, quantity, amount, top in [('QA-A service', '2', '17.50', .3), ('QA-B service', '3', '21.75', .4)]:
        source.extend([word_line(text, .05, top), word_line(quantity, .5, top), word_line(amount, .8, top)])
    cosine, sine = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    aspect = width / height
    for line in source:
        word = line['words'][0]
        left, top, right, bottom = word['region']
        corners = [(.5 + (x - .5) * cosine - (y - .5) * sine / aspect,
                    .5 + (x - .5) * sine * aspect + (y - .5) * cosine)
                   for x, y in [(left, top), (left, bottom), (right, top), (right, bottom)]]
        word['region'] = [min(x for x, _ in corners), min(y for _, y in corners),
                          max(x for x, _ in corners), max(y for _, y in corners)]
        line['region'] = box(line['words'])
    return source, definition, {'1': {'width': width, 'height': height, 'unit': 'PDF_POINT'}}


@pytest.mark.parametrize(('width', 'height', 'angle'), [(612, 792, 1), (612, 792, -1),
                                                      (792, 612, 1), (792, 612, -1)])
@pytest.mark.parametrize('pitch', [.0114, .014])
def test_local_full_literal_header_spans_preserve_rows_under_physical_skew(width, height, angle, pitch):
    source, definition, geometry = source_and_definition(width, height, angle, pitch)
    original = copy.deepcopy(source)
    result = extract(source, definition, geometry)
    assert [[row[name]['value'] for name in ['description', 'quantity', 'amount']]
            for row in result['tables']['detail']] == [
                ['QA-A service', '2', '17.50'], ['QA-B service', '3', '21.75']]
    assert result['table_unparsed']['detail'] == []
    assert source == original and result['lines'] == original


@pytest.mark.parametrize('extra', ['BODY-SKU', '001', '%'])
def test_header_neighborhood_must_preserve_every_unknown_neighbor_token(extra):
    source, definition, geometry = source_and_definition(792, 612, -1, extra=extra)
    result = extract(source, definition, geometry)
    assert result['tables']['detail'] == []
    visible = ' '.join(region['text'] for region in result['table_unparsed']['detail'])
    assert extra in visible


def test_a_complete_early_product_between_caption_baselines_is_not_header_material():
    source, definition, geometry = source_and_definition(612, 792, 0)
    source.extend([word_line('EARLY-SKU', .05, .212), word_line('2', .5, .212), word_line('17.50', .8, .212)])
    result = extract(source, definition, geometry)
    assert result['tables']['detail'] == []
    visible = ' '.join(region['text'] for region in result['table_unparsed']['detail'])
    assert all(value in visible for value in ['EARLY-SKU', '2', '17.50', 'QA-A service', 'QA-B service'])


def test_duplicate_full_caption_nearby_is_not_selected_by_first_match():
    source, definition, geometry = source_and_definition(792, 612, 0)
    source.extend([word_line('Qty', .508, .207), word_line('units', .5, .221)])
    result = extract(source, definition, geometry)
    assert result['tables']['detail'] == []
    visible = ' '.join(region['text'] for region in result['table_unparsed']['detail'])
    assert visible.count('Qty') == 2 and visible.count('units') == 2


def test_a_neighbor_glyph_overlapping_the_header_cannot_be_omitted_from_its_window():
    source, definition, geometry = source_and_definition(612, 792, 0)
    unknown = word_line('001', .3, .17)
    unknown['words'][0]['region'][3] = .202
    unknown['region'] = box(unknown['words'])
    source.append(unknown)
    result = extract(source, definition, geometry)
    assert result['tables']['detail'] == []
    assert '001' in ' '.join(region['text'] for region in result['table_unparsed']['detail'])


def test_far_literal_caption_word_does_not_extend_the_local_header_search():
    source, definition, geometry = source_and_definition(612, 792, 0)
    word = source[3]['words'][0]
    word['region'][1], word['region'][3] = .27, .28
    source[3]['region'] = box(source[3]['words'])
    result = extract(source, definition, geometry)
    assert result['tables']['detail'] == []
    assert result['table_unparsed']['detail']


def test_overlapping_header_neighborhoods_with_different_word_sets_are_both_ambiguous():
    definition = table_definition()
    definition['tables'][0]['visual']['region'] = [.04, .18, .99, .25]
    source = [word_line('Description', .05, .19), word_line('Qty', .5, .20),
              word_line('Amount', .8, .20), word_line('Description', .05, .21)]
    assert header_neighborhoods(visual_lines(source, .004), definition['tables'][0]['visual'], source, []) == []


@pytest.mark.parametrize('extra', ['001', '%'])
def test_all_string_columns_do_not_let_an_extra_value_become_a_caption(extra):
    source, definition, geometry = source_and_definition(792, 612, -1, extra=extra)
    for field in definition['tables'][0]['fields']:
        field['logical_type'] = 'STRING'
    result = extract(source, definition, geometry)
    assert result['tables']['detail'] == []
    assert extra in ' '.join(region['text'] for region in result['table_unparsed']['detail'])


@pytest.mark.parametrize('new_first', [True, False])
def test_mixed_legacy_and_new_headers_keep_document_row_order(new_first):
    _, definition, geometry = source_and_definition(612, 792, 0)
    source = []
    labels = definition['tables'][0]['visual']['header_labels']
    for index, top in enumerate([.2, .6]):
        for label, x in zip(labels, [.05, .5, .8], strict=True):
            if (index == 0) == new_first:
                first, last = label.split()
                source.extend([word_line(first, x, top), word_line(last, x, top + .014)])
            else:
                source.append(word_line(label, x, top))
        source.extend([word_line('QA-' + str(index) + ' service', .05, top + .1),
            word_line(str(index + 2), .5, top + .1), word_line(['17.50', '21.75'][index], .8, top + .1)])
    original = copy.deepcopy(source)
    result = extract(source, definition, geometry)
    assert [[row[name]['value'] for name in ['description', 'quantity', 'amount']]
            for row in result['tables']['detail']] == [
                ['QA-0 service', '2', '17.50'], ['QA-1 service', '3', '21.75']]
    assert result['table_unparsed']['detail'] == []
    assert source == original
    for index, row in enumerate(result['tables']['detail']):
        expected = {'description': 'QA-' + str(index) + ' service', 'quantity': str(index + 2),
                    'amount': ['17.50', '21.75'][index]}
        for name, value in expected.items():
            source_line = next(line for line in original if line['text'] == value)
            assert row[name]['origin']['region'] == source_line['region']
            assert row[name]['origin']['page'] == source_line['page']
