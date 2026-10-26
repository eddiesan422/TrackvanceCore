"""Fresh Builder imports and exact source identity across its read boundary."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


def test_fresh_builder_dependencies_work_after_application_reads_are_denied():
    # A separate interpreter cannot inherit a helper cached by earlier tests.
    # Denying new application-module reads models the source part of Landlock;
    # the existing Linux child test separately exercises the actual confinement.
    code = r'''
import copy
import importlib
import importlib.abc
import json
import sys
from types import SimpleNamespace

sys.path.insert(0, sys.argv[1])
from trackvance.document_discovery import discover, extract_reference
from trackvance.document_representation import VERSION, parse_table_html

html = '<table><tr><th>Code</th><th>Description</th></tr><tr><td>0017</td><td>Item<br>detail</td></tr></table>'
table = parse_table_html(html, identity='source-table')[0]
table.update(source_block_id='table-block', page_one_based=1, query_geometry=[10, 20, 900, 800], geometry_kind='TABLE_REGION')
lines = [{'text':'Invoice: SAMPLE-17', 'page':1, 'region':[0, 0, 1, 1],
          'method':'PADDLE_BLOCK', 'geometry_kind':'QUERY_REGION', 'confidence':None}]
representation = {'version':VERSION, 'source_sha256':'a'*64, 'configuration_sha256':'b'*64,
    'pages':[{'id':'source-page', 'page_one_based':1, 'stage':'PADDLE_STRUCTURED',
        'geometry':{'width':1000, 'height':1000, 'unit':'PIXEL'},
        'blocks':[{'id':'heading-block', 'type':'text', 'text':lines[0]['text'],
                   'geometry':None, 'geometry_kind':'UNKNOWN'}], 'tables':[table]}]}
reference = SimpleNamespace(id='source-reference', payload={'lines':lines, 'document_representation':representation})
original = copy.deepcopy(reference.payload)

class DenyNewApplicationReads(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('trackvance.'):
            raise PermissionError('APPLICATION_SOURCE_READ_DENIED:' + fullname)
        return None

sys.meta_path.insert(0, DenyNewApplicationReads())
try:
    importlib.import_module('trackvance.forbidden_module_probe')
except PermissionError:
    pass
else:
    raise AssertionError('Application import read boundary must remain active')
discovered = discover([reference])
mapped = extract_reference(reference, discovered['definition'])
cell = mapped['tables']['detalle'][0]['code']
assert cell['observed'] == '0017'
assert mapped['tables']['detalle'][0]['description']['observed'] == 'Item\ndetail'
header = next(iter(mapped['fields'].values()))
assert header['geometry_kind'] == 'UNKNOWN'
assert header['origin']['geometry_kind'] == 'UNKNOWN'
assert header['origin']['region_semantics'] == 'PAGE_LOCATOR_ONLY'
assert all(item['geometry'] is None for item in mapped['document_representation']['pages'][0]['tables'][0]['cells'])
assert reference.payload == original
print(json.dumps({'discovery':True, 'extraction':True, 'application_read_boundary':True, 'cell_geometry':'UNKNOWN'}))
'''
    source = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(source)],
        capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"discovery": True, "extraction": True,
        "application_read_boundary": True, "cell_geometry": "UNKNOWN"}


@pytest.mark.parametrize("helper", ["document_header_association.py", "document_candidate_selection.py"])
def test_builder_processor_identity_changes_when_semantic_helper_changes(monkeypatch, helper):
    from trackvance import document_builder_workspace as workspace

    before = workspace.processor_identity()
    path = Path(workspace.__file__).with_name(helper)
    assert before[helper] == hashlib.sha256(path.read_bytes()).hexdigest()
    original = Path.read_bytes

    def changed(candidate):
        return b"changed semantic helper source" if candidate == path else original(candidate)

    monkeypatch.setattr(Path, "read_bytes", changed)
    after = workspace.processor_identity()
    assert {name for name in before if before[name] != after[name]} == {helper}
    assert set(before) == set(after)
