"""Stopped build extraction binds exact committed source and built assets."""
import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci import frontend_build_proof, image_bundle


@pytest.mark.parametrize('tamper', [False, True])
def test_export_checks_source_and_cleans_only_stopped_owned_container(tmp_path, monkeypatch, tamper):
    sha, identifier, image = 'a' * 40, 'c' * 64, 'sha256:' + 'b' * 64
    expected = {'package.json': b'{"version":"0.8.0"}', 'src/app/App.tsx': b'version0.8.0'}
    stage = tmp_path / 'stage'
    for name, content in {**expected, 'dist/index.html': b'<app/>', 'dist/assets/index.js': b'built'}.items():
        path = stage / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    if tamper:
        (stage / 'src/app/App.tsx').write_bytes(b'other-source')
    row = {'Id': image, 'Config': {}}
    monkeypatch.setattr(frontend_build_proof, 'tracked_sources', lambda *_: expected)
    monkeypatch.setattr(image_bundle, 'inspect_image', lambda *_: row)
    calls = []

    def docker(*args):
        calls.append(args)
        if args[0] == 'create':
            return identifier
        if args[0] == 'cp':
            source = stage / args[1].split(':/app/', 1)[1]
            destination = Path(args[2])
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copyfile(source, destination)
        if args[0] == 'inspect':
            owner = next(call[call.index('--name') + 1] for call in calls if call[0] == 'create')
            return json.dumps([{'Id': identifier, 'Image': image, 'State': {'Running': False},
                               'Config': {'Labels': {'trackvance.ci.proof.owner': owner}}}])
        return ''

    monkeypatch.setattr(image_bundle, 'docker', docker)
    output = tmp_path / 'bundle'
    output.mkdir()
    if tamper:
        with pytest.raises(ValueError, match='committed bytes'):
            frontend_build_proof.export_proof(output, sha, 'build-tag')
    else:
        descriptor = frontend_build_proof.export_proof(output, sha, 'build-tag')
        with zipfile.ZipFile(output / descriptor['archive']) as archive:
            assert set(archive.namelist()) == {'source/' + name for name in expected} | {
                'dist/index.html', 'dist/assets/index.js'}
        assert descriptor['source_sha'] == sha and descriptor['build_image_id'] == image
    assert calls[-1] == ('rm', identifier)
    assert all(call[0] in {'create', 'cp', 'inspect', 'rm'} for call in calls)
    create = calls[0]
    assert create[create.index('--network') + 1] == 'none'


def test_implicit_volume_image_is_rejected_before_container_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(image_bundle, 'inspect_image', lambda *_: {'Config': {'Volumes': {'/data': {}}}})
    monkeypatch.setattr(image_bundle, 'docker', lambda *_: pytest.fail('Implicit volumes reached Docker create'))
    with pytest.raises(ValueError, match='implicit volumes'):
        frontend_build_proof.export_proof(tmp_path, 'a' * 40, 'build-tag')
