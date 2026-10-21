"""Page geometry is real bounded metadata, distinct from observed word origins."""
import copy
import math

import pytest
from test_document_skew_rows_097 import staggered_source, table_definition, word_line

from trackvance import document_engine
from trackvance.document_builder_executor import _valid_request, _valid_result
from trackvance.document_engine import extract
from trackvance.document_template_builder_rules import box, projected_region


def request(geometry):
    return {'operation': 'EXTRACT', 'definition': table_definition(),
        'references': [{'id': 'reference-1', 'lines': staggered_source(.014), 'page_geometry': geometry}]}


@pytest.mark.parametrize('geometry', [
    {'1': {'width': 612, 'height': 792, 'unit': 'PDF_POINT'}},
    {'1': {'width': 3600, 'height': 2400, 'unit': 'PIXEL'}}])
def test_real_page_dimensions_roundtrip_in_builder_payload_and_protocol(geometry):
    original = copy.deepcopy(geometry)
    data = request(geometry)
    assert _valid_request(data)
    payload = extract(data['references'][0]['lines'], data['definition'], geometry)
    assert payload['page_geometry'] == geometry
    assert _valid_result({'reference-1': payload}, data)
    assert geometry == original
    payload['page_geometry']['1']['width'] += 1
    assert geometry == original


@pytest.mark.parametrize('geometry', [None, [], {'01': {'width': 1, 'height': 1, 'unit': 'PIXEL'}},
    {'1': {'width': 0, 'height': 792, 'unit': 'PDF_POINT'}},
    {'1': {'width': float('inf'), 'height': 792, 'unit': 'PDF_POINT'}},
    {'1': {'width': float('nan'), 'height': 792, 'unit': 'PDF_POINT'}},
    {'1': {'width': True, 'height': 792, 'unit': 'PDF_POINT'}},
    {'1': {'width': 20000, 'height': 792, 'unit': 'PDF_POINT'}},
    {'1': {'width': 10 ** 1000, 'height': 792, 'unit': 'PDF_POINT'}},
    {'1': {'width': 10 ** 1000, 'height': 792, 'unit': 'PIXEL'}},
    {'1': {'width': 1e-320, 'height': 792, 'unit': 'PDF_POINT'}},
    {'1': {'width': 10000, 'height': 10000, 'unit': 'PIXEL'}},
    {'1': {'width': 612, 'height': 792, 'unit': 'ASSUMED_A4'}},
    {'1': {'width': 612, 'height': 792, 'unit': []}},
    {'1' * 10000: {'width': 612, 'height': 792, 'unit': 'PDF_POINT'}},
    {'2': {'width': 612, 'height': 792, 'unit': 'PDF_POINT'}},
    {str(page): {'width': 1, 'height': 1, 'unit': 'PIXEL'} for page in range(1, 52)}])
def test_builder_refuses_invalid_or_unrelated_page_geometry(geometry):
    assert not _valid_request(request(geometry))
    if geometry is not None:
        with pytest.raises(ValueError, match='DOCUMENT_PAGE_GEOMETRY'):
            extract(staggered_source(.014), table_definition(), geometry)


def test_legacy_reference_without_dimensions_remains_supported_without_fabricating_them():
    data = request({})
    data['references'][0].pop('page_geometry')
    assert _valid_request(data)
    payload = extract(data['references'][0]['lines'], data['definition'])
    assert 'page_geometry' not in payload
    assert _valid_result({'reference-1': payload}, data)
    payload['page_geometry'] = {'1': {'width': 612, 'height': 792, 'unit': 'PDF_POINT'}}
    assert not _valid_result({'reference-1': payload}, data)


def test_protocol_rejects_an_added_null_geometry_for_a_legacy_reference():
    data = request({})
    data['references'][0].pop('page_geometry')
    payload = extract(data['references'][0]['lines'], data['definition'])
    payload['page_geometry'] = None
    assert not _valid_result({'reference-1': payload}, data)


def test_protocol_rejects_boolean_geometry_even_when_python_equals_source_integer():
    data = request({'1': {'width': 1, 'height': 792, 'unit': 'PIXEL'}})
    payload = extract(data['references'][0]['lines'], data['definition'], data['references'][0]['page_geometry'])
    payload['page_geometry']['1']['width'] = True
    assert not _valid_result({'reference-1': payload}, data)


def test_protocol_does_not_accept_changed_page_dimensions_or_drop_source_metadata():
    geometry = {'1': {'width': 612, 'height': 792, 'unit': 'PDF_POINT'}}
    data = request(geometry)
    payload = extract(data['references'][0]['lines'], data['definition'], geometry)
    changed = copy.deepcopy(payload)
    changed['page_geometry']['1']['width'] = 792
    assert not _valid_result({'reference-1': changed}, data)
    payload.pop('page_geometry')
    assert not _valid_result({'reference-1': payload}, data)


def test_pdf_page_dimensions_are_measured_individually_without_changing_word_geometry(monkeypatch, tmp_path):
    xml = '<doc><page width="612" height="792"><flow><line xMin="61.2" yMin="79.2" xMax="122.4" yMax="95.04"><word xMin="61.2" yMin="79.2" xMax="122.4" yMax="95.04">FIRST</word></line></flow></page><page width="792" height="612"><flow><line xMin="79.2" yMin="61.2" xMax="158.4" yMax="73.44"><word xMin="79.2" yMin="61.2" xMax="158.4" yMax="73.44">SECOND</word></line></flow></page></doc>'

    def command(args):
        return ('Pages: 2\nEncrypted: no' if args[0].endswith('pdfinfo')
                else xml if args[0].endswith('pdftotext') else '')

    monkeypatch.setattr(document_engine, 'command', command)
    source = tmp_path / 'generic.pdf'
    original, pages = document_engine.lines_from_file(source, tmp_path, tmp_path / 'old-progress')
    geometry = {}
    current, count = document_engine.lines_from_file(source, tmp_path, tmp_path / 'new-progress', geometry)
    assert pages == count == 2
    assert current == original
    assert geometry == {'1': {'width': 612, 'height': 792, 'unit': 'PDF_POINT'},
                        '2': {'width': 792, 'height': 612, 'unit': 'PDF_POINT'}}
    assert current[0]['region'] == pytest.approx([.1, .1, .2, .12])


def rotated_source(width, height, angle):
    ratio, radians = width / height, math.radians(angle)
    cosine, sine = math.cos(radians), math.sin(radians)
    lines = [word_line(label, x, .2) for label, x in
             [('Description', .05), ('Qty', .5), ('Amount', .8)]]
    for code, quantity, amount, y in [('QA-A', '2', '17.50', .3), ('QA-END', '3', '21.75', .8)]:
        lines.extend([word_line(code, .05, y), word_line('service', .435, y),
                      word_line(quantity, .5, y + .012), word_line(amount, .8, y + .012)])
    for line in lines:
        word = line['words'][0]
        word['region'][2] = word['region'][0] + .02
        x1, y1, x2, y2 = word['region']
        corners = [(.5 + (x - .5) * cosine - (y - .5) * sine / ratio,
                    .5 + (x - .5) * sine * ratio + (y - .5) * cosine)
                   for x, y in [(x1, y1), (x1, y2), (x2, y1), (x2, y2)]]
        word['region'] = [min(x for x, _ in corners), min(y for _, y in corners),
                          max(x for x, _ in corners), max(y for _, y in corners)]
        line['region'] = box(line['words'])
    return lines


@pytest.mark.parametrize(('width', 'height', 'angle'), [(612, 792, 1), (612, 792, -1),
    (792, 612, .6), (792, 612, -.6), (1800, 3600, 1.5), (3600, 1800, -.35)])
def test_real_aspect_ratio_corrects_both_axes_without_rewriting_any_original_origin(width, height, angle):
    source = rotated_source(width, height, angle)
    before = copy.deepcopy(source)
    geometry = {'1': {'width': width, 'height': height, 'unit': 'PIXEL'}}
    result = extract(source, table_definition(), geometry)
    assert [[row[key]['value'] for key in ['description', 'quantity', 'amount']]
            for row in result['tables']['detail']] == [
                ['QA-A service', '2', '17.50'], ['QA-END service', '3', '21.75']]
    assert not result['table_unparsed']['detail']
    for index, row in enumerate(result['tables']['detail']):
        for offset, key in [(2, 'quantity'), (3, 'amount')]:
            assert row[key]['origin']['region'] == source[3 + index * 4 + offset]['region']
    assert source == before and result['lines'] == before


def test_temporal_projection_does_not_clamp_coordinates_or_change_the_source_box():
    word = {'text': 'EDGE', 'region': [.001, .8, .02, .82]}
    before = copy.deepcopy(word)
    frame = {'pivot': [.1, .2], 'slope': -.05, 'aspect': .5,
             'cosine': math.cos(-.1), 'sine': math.sin(-.1)}
    assert projected_region(word, frame)[0] < 0
    assert word == before


def test_pdf_point_and_pixel_dimensions_with_the_same_real_aspect_have_identical_association():
    source = rotated_source(612, 792, -1)
    point = extract(source, table_definition(), {'1': {'width': 612, 'height': 792, 'unit': 'PDF_POINT'}})
    pixel = extract(source, table_definition(), {'1': {'width': 2448, 'height': 3168, 'unit': 'PIXEL'}})
    assert point['tables'] == pixel['tables']
    assert point['table_unparsed'] == pixel['table_unparsed'] == {'detail': []}


def test_native_reference_read_geometry_is_bound_to_new_builder_snapshot_and_result(authenticated, tmp_path):
    from test_document_builder_097 import create_workspace

    from trackvance.document_service import digest

    _, ids, draft = create_workspace(authenticated, tmp_path)
    measured = {}
    for identity in ids:
        response = authenticated.get(f'/api/v1/documents/builder/references/{identity}')
        assert response.status_code == 200
        payload = response.json()['payload']
        assert payload['page_geometry'] == {'1': {'width': 612, 'height': 792, 'unit': 'PDF_POINT'}}
        measured[identity] = payload['page_geometry']
    response = authenticated.post(f'/api/v1/documents/builder/drafts/{draft["id"]}/test',
        json={'expected_version': draft['version']})
    assert response.status_code == 200, response.text
    tested = response.json()
    for reference in tested['snapshot']['references']:
        assert reference['page_geometry'] == measured[reference['id']]
        assert tested['automatic_results'][reference['id']]['page_geometry'] == reference['page_geometry']
    changed = copy.deepcopy(tested['snapshot'])
    changed['references'][0]['page_geometry']['1']['width'] = 792
    assert digest(changed) != tested['snapshot_hash']
