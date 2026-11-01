"""Diagnostic CPU transport for the existing native PDF reader, without app startup.

Uses an already present backend image, no network/GPU, two read-only source mounts,
bounded tmpfs, and an exact owned auto-removed container. No DB or model calls.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

BOOT = r'''
import base64, hashlib, importlib.util, json, os, sys, tempfile
from pathlib import Path
p=json.load(sys.stdin)
spec=importlib.util.spec_from_file_location('native_control','/diagnostics/control_native.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
assert hashlib.sha256(Path('/diagnostics/control_native.py').read_bytes()).hexdigest()==p['helper_sha256']
print(json.dumps({'kind':'OWNED_CPU_START','hostname':os.uname().nodename,'pid':os.getpid(),'models':0,'DB':0}),flush=True)
for d in p['documents']:
 with tempfile.TemporaryDirectory(prefix='documentary-native-') as tmp:
  f=Path(tmp)/'document.pdf';f.write_bytes(base64.b64decode(d['source_b64'],validate=True))
  assert hashlib.sha256(f.read_bytes()).hexdigest()==d['source_sha256']
  result=m.control(f,d['document_id'],d['modality'],'/source')
  print(json.dumps({'kind':'RESULT','result':result},ensure_ascii=False),flush=True)
'''


def canonical(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--manifest',type=Path,required=True)
    p.add_argument('--worktree',type=Path,required=True)
    p.add_argument('--image',required=True)
    p.add_argument('--output-dir',type=Path,required=True)
    a=p.parse_args()
    if not a.image.startswith('sha256:'):
        raise ValueError('IMMUTABLE_EXISTING_IMAGE_REQUIRED')
    manifest=json.loads(a.manifest.read_bytes())
    scripts=Path(__file__).resolve().parent
    packet={'helper_sha256':hashlib.sha256((scripts/'control_native.py').read_bytes()).hexdigest(),'documents':[]}
    for d in manifest['documents']:
        source=Path(d['source_file']);raw=source.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=d['source_sha256']:
            raise ValueError('SOURCE_CHANGED:'+d['document_id'])
        packet['documents'].append({'document_id':d['document_id'],'modality':d['modality'],
            'source_sha256':d['source_sha256'],'source_b64':base64.b64encode(raw).decode()})
    if len(packet['documents'])!=30:
        raise ValueError('EXACT30_REQUIRED')
    out=a.output_dir;out.mkdir()
    name='trackvance-doc-eval-native-'+uuid.uuid4().hex[:12]
    cmd=['docker','--context','default','run','--pull','never','--rm','-i',
         '--name',name,'--cidfile',str((out/'owned.cid').resolve()),
         '--label','io.trackvance.diagnostic-owner=documentary-evaluation-20261101',
         '--network','none','--read-only','--memory','1g','--cpus','2','--pids-limit','128',
         '--cap-drop','ALL','--security-opt','no-new-privileges',
         '--tmpfs','/tmp:rw,noexec,nosuid,size=256m',
         '--mount',f'type=bind,source={(a.worktree/"backend/src").resolve()},target=/source/backend/src,readonly',
         '--mount',f'type=bind,source={scripts},target=/diagnostics,readonly',
         '--entrypoint','python',a.image,'-I','-B','-c',BOOT]
    env={k:v for k,v in os.environ.items() if k not in {'DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'}}
    (out/'intent.private.json').write_bytes(canonical({'name':name,'command':cmd,'model_calls':0,'DB_writes':0,'sources':30})+b'\n')
    start=time.monotonic()
    with (out/'transport.stdout.ndjson').open('xb') as stdout,(out/'transport.stderr.txt').open('xb') as stderr:
        process=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=stdout,stderr=stderr,env=env,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            process.communicate(canonical(packet),timeout=300)
        except subprocess.TimeoutExpired:
            process.kill();process.wait(timeout=5)
            cidpath=out/'owned.cid'
            if cidpath.is_file():
                owned=cidpath.read_text().strip()
                found=subprocess.run(['docker','--context','default','inspect',owned],env=env,capture_output=True,timeout=10)
                if found.returncode==0:
                    row=json.loads(found.stdout)[0]
                    if row['Name']!='/'+name or row['Config']['Labels'].get('io.trackvance.diagnostic-owner')!='documentary-evaluation-20261101':
                        raise RuntimeError('CPU_CLEANUP_OWNER_MISMATCH')
                    subprocess.run(['docker','--context','default','stop','--time','10',owned],env=env,capture_output=True,check=True,timeout=20)
            raise RuntimeError('CPU_CONTROL_TIMEOUT_OWNED_PROCESS_STOPPED_NO_RETRY')
    if process.returncode!=0:
        raise RuntimeError('OWNED_CPU_CONTROL_FAILED_INSPECT_PRESERVED_STDERR')
    frames=[json.loads(x) for x in (out/'transport.stdout.ndjson').read_bytes().splitlines()]
    cid=(out/'owned.cid').read_text().strip()
    if frames[0]['hostname']!=cid[:12]:
        raise RuntimeError('OWNED_CPU_IDENTITY_MISMATCH')
    results=[x['result'] for x in frames if x['kind']=='RESULT']
    if len(results)!=30 or {d['document_id'] for d in results}!={d['document_id'] for d in manifest['documents']}:
        raise RuntimeError('EXACT_CONTROL_MATRIX_REQUIRED')
    for r in results:
        with (out/(r['document_id']+'.json')).open('xb') as stream:
            stream.write(canonical(r)+b'\n')
    check=subprocess.run(['docker','--context','default','ps','-aq','--no-trunc','--filter','id='+cid],env=env,capture_output=True,check=True)
    if check.stdout.strip():
        raise RuntimeError('OWNED_CPU_CONTAINER_NOT_REMOVED')
    summary={'status':'NATIVE_READER_CONTROL_MATRIX_COMPLETE','rows':30,'executed_digital':15,'scanned_not_applicable':15,
             'statuses':{s:sum(r['status']==s for r in results) for s in sorted({r['status'] for r in results})},
             'model_calls':0,'DB_writes':0,'container_id':cid,'auto_removed_verified':True,
             'controller_elapsed_seconds':time.monotonic()-start,'image':a.image}
    (out/'summary.private.json').write_bytes(canonical(summary)+b'\n')
    print(json.dumps(summary))


if __name__=='__main__':
    main()
