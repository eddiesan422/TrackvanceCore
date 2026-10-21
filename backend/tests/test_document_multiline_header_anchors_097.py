"""Full literal table captions may span baselines; partial anchors stay partial."""
import copy

import pytest
from test_document_skew_rows_097 import table_definition, word_line

from trackvance.document_engine import extract
from trackvance.document_template_builder_rules import box


def caption_source():
    source = [word_line('Description', .05, .2), word_line('Discount', .515, .2),
        word_line('%', .5, .21), word_line('Tax', .815, .2), word_line('%', .8, .21)]
    for code, discount, tax, y in [('QA-A', '7%', '19%', .3), ('QA-B', '5%', '0%', .4)]:
        source.extend([word_line(code, .05, y), word_line(discount, .5, y), word_line(tax, .8, y)])
    for line in source:
        if line['words'][0]['text'] == '%':
            line['words'][0]['region'][2] = line['words'][0]['region'][0] + .025
            line['region'] = box(line['words'])
    return source


def caption_definition():
    definition = table_definition()
    table = definition['tables'][0]
    table['fields'][1].update(name='discount', label='Discount %', logical_type='STRING')
    table['fields'][2].update(name='tax', label='Tax %', logical_type='STRING')
    table['visual']['header_labels'] = ['Description', 'Discount %', 'Tax %']
    return definition


@pytest.mark.parametrize('method', ['DIGITAL', 'OCR'])
def test_full_literal_headers_follow_vertical_reading_order_inside_one_column(method):
    source = caption_source()
    for line in source:
        line['method'] = method
    original = copy.deepcopy(source)
    result = extract(source, caption_definition())
    assert [[row[key]['value'] for key in ['description', 'discount', 'tax']]
            for row in result['tables']['detail']] == [
                ['QA-A', '7%', '19%'], ['QA-B', '5%', '0%']]
    assert not result['table_unparsed']['detail']
    assert source == original


def test_full_literal_caption_preserves_a_slash_split_over_baselines():
    definition = table_definition()
    definition['tables'][0]['fields'][1]['label'] = 'Hours / units'
    definition['tables'][0]['visual']['header_labels'][1] = 'Hours / units'
    source = [word_line('Description', .05, .2), word_line('Hours', .515, .2),
        word_line('/', .60, .2), word_line('units', .5, .21), word_line('Amount', .8, .2),
        word_line('QA-A', .05, .3), word_line('2', .5, .3), word_line('17.50', .8, .3)]
    result = extract(source, definition)
    assert [[row[key]['value'] for key in ['description', 'quantity', 'amount']]
            for row in result['tables']['detail']] == [['QA-A', '2', '17.50']]
    assert result['table_unparsed']['detail'] == []


@pytest.mark.parametrize('extra', ['001', '%', 'UNMAPPED'])
def test_partial_visual_anchor_does_not_expand_from_a_full_field_label_or_drop_a_string(extra):
    definition = table_definition()
    definition['tables'][0]['fields'][0]['label'] = 'Description ' + extra
    source = [word_line('Description', .05, .2), word_line('Qty', .5, .2), word_line('Amount', .8, .2),
        word_line(extra, .05, .21), word_line('Service', .05, .3),
        word_line('2', .5, .3), word_line('17.50', .8, .3)]
    result = extract(source, definition)
    visible = ' '.join(region['text'] for region in result['table_unparsed']['detail'])
    assert extra in visible


def test_vertical_words_with_disjoint_horizontal_support_are_not_one_caption():
    source = caption_source()
    source[2]['words'][0]['region'][0] = .47
    source[2]['words'][0]['region'][2] = .49
    source[2]['region'] = box(source[2]['words'])
    result = extract(source, caption_definition())
    assert result['tables']['detail'] == []
    assert result['table_unparsed']['detail']


def test_same_baseline_words_cannot_be_permuted_to_satisfy_a_different_label():
    source = caption_source()
    for index in [2, 4]:
        source[index]['words'][0]['region'][1:4:2] = [.2, .206]
        source[index]['region'] = box(source[index]['words'])
    result = extract(source, caption_definition())
    assert result['tables']['detail'] == []
    assert result['table_unparsed']['detail']


@pytest.mark.parametrize('extra', ['%', '001', 'UNMAPPED'])
def test_an_extra_string_inside_the_extended_header_is_not_silently_omitted(extra):
    source = caption_source()
    source.append(word_line(extra, .535, .208))
    result = extract(source, caption_definition())
    assert result['tables']['detail'] == []
    assert result['table_unparsed']['detail']


def test_duplicate_vertical_caption_tokens_are_ambiguous_and_remain_visible():
    source = caption_source()
    source.append(word_line('%', .5, .207))
    source[-1]['words'][0]['region'][2] = .525
    source[-1]['region'] = box(source[-1]['words'])
    result = extract(source, caption_definition())
    assert result['tables']['detail'] == []
    assert result['table_unparsed']['detail']


def test_literal_caption_matching_does_not_invent_an_absent_slash():
    definition = caption_definition()
    definition['tables'][0]['visual']['header_labels'][1] = 'Discount / %'
    result = extract(caption_source(), definition)
    assert result['tables']['detail'] == []
    assert result['table_unparsed']['detail']


def test_an_unused_vertical_alternative_cannot_relax_duplicate_horizontal_headers():
    definition = table_definition()
    definition['tables'][0]['visual'].update(region=[.20, .20, .99, .23],
        header_labels=['Description', 'Qty units', 'Amount'], column_bounds=[0, .4, .7, 1])
    definition['tables'][0]['fields'][1]['label'] = 'Qty units'
    source = [word_line(text, x, y) for text, x, y in [
        ('Amount', .01, .209), ('units', .05, .210), ('Qty', .06, .200),
        ('Description', .20, .209), ('BODY-SKU', .40, .209),
        ('Qty', .50, .209), ('units', .56, .209), ('Amount', .80, .209),
        ('QA-A', .20, .300), ('2', .50, .300), ('17.50', .80, .300)]]
    for line in source:
        line['words'][0]['region'][2] = line['words'][0]['region'][0] + .02
        line['region'] = box(line['words'])
    result = extract(source, definition)
    assert result['tables']['detail'] == []
    visible = ' '.join(item['text'] for item in result['table_unparsed']['detail'])
    assert all(value in visible for value in ['BODY-SKU', 'QA-A', '2', '17.50'])
