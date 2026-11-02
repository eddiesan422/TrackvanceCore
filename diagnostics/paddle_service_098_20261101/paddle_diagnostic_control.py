"""Bounded host control for one isolated, instrumented Paddle diagnosis.

All private paths/identities/request bytes are pinned external inputs. Existing
preservation functions are imported from their explicitly hashed public source;
no historical campaign CLI or model helper is executed.
"""
from __future__ import annotations
import argparse,base64,hashlib,importlib.util,json,os,queue,subprocess,sys,threading,time,traceback
from pathlib import Path

def need(value,reason):
    if not value:raise RuntimeError(reason)
def canonical(x):return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def pin(p):
    p=Path(p).resolve();return {'path':str(p),'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
def load_pin(x):
    need(pin(x['path'])==x,'PIN_CHANGED');return json.loads(Path(x['path']).read_bytes())
def save(p,x):
    with Path(p).open('xb') as f:f.write(canonical(x)+b'\n');f.flush();os.fsync(f.fileno())
    return pin(p)
def module(name,p):
    spec=importlib.util.spec_from_file_location(name,p);m=importlib.util.module_from_spec(spec);sys.modules[name]=m;spec.loader.exec_module(m);return m
def record(p,kind,**x):
    raw=canonical({'kind':kind,'at_epoch':time.time(),'at_monotonic':time.monotonic(),**x})+b'\n'
    with Path(p).open('ab') as f:f.write(raw);f.flush();os.fsync(f.fileno())
def elapsed(clock):return time.monotonic()-clock['started_monotonic']
def remaining(clock):
    need(abs((time.time()-clock['started_epoch'])-elapsed(clock))<30,'CAMPAIGN_CLOCK_DISCONTINUITY')
    return 900-elapsed(clock)

def control_necessity(result,actual_child_spawns):
    """A control distinguishes a child failure; never hunt a failure after success."""
    need(result.get('resources_released') is True,'CLOSURE_UNCONFIRMED_STOP_ALL_GPU')
    if result.get('status')=='COMPLETE':return False,'F02_COMPLETE_NO_FAILURE_HUNT'
    if not actual_child_spawns:return False,'F02_PARENT_FAILURE_BEFORE_PADDLE_CHILD_CONTROL_NOT_NEEDED'
    return True,'F02_CHILD_FAILURE_OR_TIMEOUT_CLOSED_ONE_COMPARATIVE_CONTROL'

def stage_file(control,rt,source,target,expected):
    """Write under the container's own uid on /tmp; keep transfer errors private."""
    raw=Path(source).read_bytes();need(hashlib.sha256(raw).hexdigest()==expected,'STAGING_SOURCE_CHANGED')
    code="""from pathlib import Path
import os,sys,hashlib
p=Path(TARGET)
assert p.parent.name.startswith('trackvance-paddle-diag-') and p.parent.parent==Path('/tmp')
assert not p.parent.is_symlink() and p.parent.lstat().st_uid==os.getuid()
raw=sys.stdin.buffer.read(1048577)
assert len(raw)<=1048576 and hashlib.sha256(raw).hexdigest()==EXPECTED
fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
assert hashlib.sha256(p.read_bytes()).hexdigest()==EXPECTED
print('STAGED_EXACT_SOURCE')
""".replace('TARGET',repr(target)).replace('EXPECTED',repr(expected))
    args=['docker','--context','default','exec','-i',rt['gateway_id'],rt['worker_python'],'-I','-B','-c',code]
    r=subprocess.run(args,input=raw,capture_output=True,timeout=30,env={k:v for k,v in os.environ.items() if k not in control.OVERRIDES},creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    return {'exit_code':r.returncode,'stdout':r.stdout.decode('utf-8','replace'),'stderr':r.stderr.decode('utf-8','replace'),'source_sha256':expected,'source_bytes':len(raw),'target':target}

def run(freeze,control,out,clock):
    F=freeze;rt=F['runtime'];out.mkdir();ledger=out/'ledger.ndjson'
    policy=module('paddle_diagnostic_policy',F['policy']['path']);guard=policy.AdmissionGuard()
    docs=load_pin(F['documents'])['documents'];need([d['document_id'] for d in docs]==['F02-02','F01-02'],'EXACT_TWO_AUTHORIZED_DOCUMENTS')
    for d in docs:
        need(pin(d['source']['path'])==d['source'],'ORIGINAL_DOCUMENT_CHANGED')
    target=F['remote_worker_directory'];need(target.startswith('/tmp/trackvance-paddle-diag-') and len(target.rsplit('-',1)[-1])==32,'OWNED_TEMP_PATH')
    linux_receipt=json.loads((Path(F['directory'])/'linux-cpu/receipt.private.json').read_bytes())
    need(linux_receipt['status']=='PASS' and linux_receipt['tests']==17 and linux_receipt['skipped']==0,'LINUX_CPU_INSTRUMENTATION_REQUIRED')
    verify="from pathlib import Path;import hashlib;print(hashlib.sha256(Path("+repr(target+'/paddle_service_worker.py')+").read_bytes()).hexdigest())"
    need(control.docker('exec',rt['gateway_id'],rt['worker_python'],'-I','-B','-c',verify).decode().strip()==F['worker']['sha256'],'REMOTE_WORKER_CHANGED')
    argv=['docker','--context','default','exec','-i',rt['gateway_id'],rt['worker_python'],'-I','-B',target+'/paddle_service_worker.py']
    process=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        env={k:v for k,v in os.environ.items() if k not in control.OVERRIDES},creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    events=queue.Queue(maxsize=64)
    def stdout_reader():
        try:
            with (out/'worker.stdout.ndjson').open('xb') as f:
                while True:
                    raw=process.stdout.readline(128*1024**2+1)
                    if not raw:break
                    f.write(raw);f.flush()
                    if len(raw)>128*1024**2:raise ValueError('WORKER_LINE_LIMIT')
                    events.put(json.loads(raw))
        except BaseException as e:events.put({'kind':'HOST_READER_FAILURE','error_type':type(e).__name__,'reason':str(e)})
        finally:events.put({'kind':'HOST_WORKER_EOF'})
    def stderr_reader():
        with (out/'worker.stderr.txt').open('xb') as f:
            while raw:=process.stderr.read(65536):f.write(raw);f.flush()
    readers=[threading.Thread(target=stdout_reader,daemon=True),threading.Thread(target=stderr_reader,daemon=True)]
    for t in readers:t.start()
    def send(x):process.stdin.write(canonical(x)+b'\n');process.stdin.flush()
    def until(kind,seconds):
        deadline=time.monotonic()+min(seconds,max(0,remaining(clock)-12))
        observed=[]
        while time.monotonic()<deadline:
            try:x=events.get(timeout=min(1,max(.001,deadline-time.monotonic())))
            except queue.Empty:continue
            observed.append(x)
            if x.get('kind')==kind:return x,observed
            need(x.get('kind') not in {'HOST_READER_FAILURE','HOST_WORKER_EOF','FATAL'},'WORKER_PROTOCOL_TERMINATED_'+str(x.get('kind')))
        raise TimeoutError('HOST_WAIT_'+kind)
    record(ledger,'RUN_STARTED',max_documents=2,Qwen_calls=0,retries=0,campaign_clock=clock)
    summary={'rows':[],'Qwen_calls':0,'retries':0,'admission':'OPEN','resource_closure_accredited':False}
    try:
        send({'startup_files_sha256':rt['startup_files_sha256'],'campaign_remaining_seconds':remaining(clock)-12,
              'allowed_documents':['F02-02','F01-02'],'document_sha256':{d['document_id']:d['source']['sha256'] for d in docs}})
        ready,_=until('READY',180);save(out/'shadow-ready.private.json',ready)
        print(json.dumps({'phase':'SHADOW_READY_NO_DOCUMENT_ADMITTED','campaign_elapsed_seconds':round(elapsed(clock),3)}),flush=True)
        for index,d in enumerate(docs):
            if index:
                previous=summary['rows'][0];r=previous['result_observation']
                spawned=previous.get('actual_paddle_child_spawns',0)
                necessary,reason=control_necessity(r,spawned)
                if not necessary:
                    record(ledger,'CONTROL_NOT_RUN',document_id=d['document_id'],reason=reason)
                    summary['rows'].append({'document_id':d['document_id'],'executed':False,'status':'NOT_RUN_NOT_NECESSARY','reason':reason});break
            need(guard.can_admit,'DIAGNOSTIC_GUARD_SUSPENDED')
            if remaining(clock)<=225 and index:
                record(ledger,'CONTROL_NOT_RUN',document_id=d['document_id'],reason='CAMPAIGN_BUDGET_RETAINED_NO_NEW_DISPATCH')
                summary['rows'].append({'document_id':d['document_id'],'executed':False,'status':'NOT_RUN_BUDGET','reason':'CAMPAIGN_BUDGET_RETAINED_NO_NEW_DISPATCH'});break
            need(remaining(clock)>225,'CAMPAIGN_INSUFFICIENT_BUDGET_NO_DISPATCH')
            source=Path(d['source']['path']).read_bytes();need(hashlib.sha256(source).hexdigest()==d['source']['sha256'],'SOURCE_CHANGED_BEFORE_ADMISSION')
            request={'request_id':d['request_id'],'attempt_id':d['attempt_id'],'filename':'document.pdf','source':base64.b64encode(source).decode(),
                'source_sha256':d['source']['sha256'],'strategy':'PADDLE_STRUCTURED','configuration':d['configuration'],
                'configuration_sha256':d['configuration_sha256'],'definition':d['definition'],'definition_sha256':d['definition_sha256'],'request_budget_seconds':185}
            record(ledger,'DOCUMENT_ADMITTED_NO_REFUND',document_id=d['document_id'],request_id=d['request_id'],source_sha256=d['source']['sha256'],request_sha256=hashlib.sha256(canonical(request)).hexdigest(),document_attempt=index+1,work_seconds=180,cleanup_seconds=30)
            print(json.dumps({'phase':'PADDLE_DOCUMENT_ADMITTED','document_id':d['document_id'],'attempt':index+1,'retries':0}),flush=True)
            started=time.monotonic();send({'kind':'RUN','document_id':d['document_id'],'a_request':request,'work_seconds':180})
            event,stream=until('RESULT',215);r=event.get('result',event.get('payload',{}));need(isinstance(r,dict),'RESULT_OBJECT_REQUIRED')
            # Runtime category is only accredited after reviewing the original trace.
            # Admission remains conservative UNKNOWN for opaque runtime responses.
            decision=guard.observe(policy.Observation(status=r['status'],resources_released=r.get('resources_released'),failure_code=r.get('failure_code'),http_status=r.get('http_status'))).to_dict()
            actual_spawns=sum(x.get('kind')=='CHILD_SPAWN' for x in stream)
            row={'document_id':d['document_id'],'executed':True,'status':r['status'],'result_observation':r,'diagnostic_admission_decision':decision,
                'actual_paddle_child_spawns':actual_spawns,'controller_elapsed_seconds':round(time.monotonic()-started,6)}
            row['saved_result']=save(out/(d['document_id']+'.result.private.json'),row)
            summary['rows'].append(row);record(ledger,'DOCUMENT_FINISHED',document_id=d['document_id'],status=r['status'],actual_paddle_child_spawns=actual_spawns,resources_released=r.get('resources_released'),decision=decision)
            print(json.dumps({'phase':'PADDLE_DOCUMENT_FINISHED','document_id':d['document_id'],'status':r['status'],'resources_released':r.get('resources_released'),'child_spawns':actual_spawns}),flush=True)
            need(r.get('resources_released') is True,'CLOSURE_UNCONFIRMED_STOP_ALL_GPU')
        send({'kind':'CLOSE'});closed,_=until('WORKER_CLOSED',30);need(closed.get('effective_close') is True,'DIAGNOSTIC_SERVER_CLOSE_NOT_ACCREDITED');save(out/'shadow-closed.private.json',closed)
        process.stdin.close();need(process.wait(timeout=min(15,max(1,remaining(clock)-10)))==0,'DIAGNOSTIC_WORKER_NONZERO')
        for t in readers:t.join(timeout=5);need(not t.is_alive(),'HOST_CAPTURE_READER_NOT_JOINED')
        summary.update(admission='CLOSED',resource_closure_accredited=True,document_attempts=sum(x['executed'] for x in summary['rows']),actual_paddle_child_spawns=sum(x.get('actual_paddle_child_spawns',0) for x in summary['rows']),GPU_campaign_elapsed_before_stop_seconds=round(elapsed(clock),6))
        record(ledger,'RUN_CLOSED',document_attempts=summary['document_attempts'],resources_released=True,Qwen_calls=0,retries=0)
        # Remove only this exact verified script and now-empty owned directory.
        cleanfiles={'paddle_service_worker.py':F['worker']['sha256'],'test_paddle_service_worker.py':F['worker_tests']['sha256']}
        cleanup="""from pathlib import Path
import os,hashlib,stat
p=Path(TARGET)
assert not p.is_symlink() and p.lstat().st_uid==os.getuid()
expected=EXPECTED
assert set(x.name for x in p.iterdir())==set(expected)
for name,wanted in expected.items():
 f=p/name
 assert not f.is_symlink() and f.is_file() and hashlib.sha256(f.read_bytes()).hexdigest()==wanted
for name in expected:(p/name).unlink()
p.rmdir()
for item in __OWNED_RECORDS__:
 path=Path(item['path'])
 assert str(path) in {'/tmp/trackvance-paddle-service-diagnosis.lock','/tmp/trackvance-paddle-service-diagnosis.active.json'}
 if not path.exists():continue
 info=path.lstat()
 assert not path.is_symlink() and stat.S_ISREG(info.st_mode)
 assert (info.st_dev,info.st_ino,info.st_uid)==(item['device'],item['inode'],item['uid'])
 path.unlink()
print('OWNED_WORKER_TEMP_AND_LOCK_REMOVED')
""".replace('TARGET',repr(target)).replace('EXPECTED',repr(cleanfiles)).replace('__OWNED_RECORDS__',repr(closed['owned_temporaries']))
        summary['own_script_temp_cleanup']=control.docker('exec',rt['gateway_id'],rt['worker_python'],'-I','-B','-c',cleanup).decode().strip()
        save(out/'summary.private.json',summary);return summary
    except BaseException as error:
        summary.update(admission='CLOSED_UNCONFIRMED_NO_MORE_GPU',error_type=type(error).__name__,traceback=traceback.format_exc())
        save(out/'controller-failure.private.json',summary);record(ledger,'RUN_ABORTED_NO_RETRY',error_type=type(error).__name__,resources_released=False)
        if process.stdin:
            try:process.stdin.close()
            except OSError:pass
        try:process.wait(timeout=min(5,max(1,remaining(clock)-10)))
        except subprocess.TimeoutExpired:
            record(ledger,'DIAGNOSTIC_WORKER_EXIT_UNCONFIRMED',resources_released=False)
        for t in readers:t.join(timeout=1)
        raise

def main():
    p=argparse.ArgumentParser();p.add_argument('--freeze',required=True,type=Path);p.add_argument('--freeze-sha256',required=True);p.add_argument('--mode',choices=['verify','start','linux-cpu','run','stop'],required=True);a=p.parse_args()
    need(pin(a.freeze)['sha256']==a.freeze_sha256,'FROZEN_MANIFEST_CHANGED');F=json.loads(a.freeze.read_bytes());D=Path(F['directory'])
    for x in F['source_pins']:need(pin(x['path'])==x,'CODE_OR_INPUT_PIN_CHANGED')
    need(F['authorized'] is True,'CURRENT_AUTHORIZATION_REQUIRED');need(pin(F['preservation_library']['path'])==F['preservation_library'],'PRESERVATION_LIBRARY_CHANGED')
    control=module('paddle_preservation_library',F['preservation_library']['path']);rt=F['runtime'];entry=load_pin(F['entry']);plan=load_pin(F['manager_plan'])
    if a.mode=='verify':
        out=D/'runtime-source-check-r01';out.mkdir();x={'inventory':save(out/'inventory.private.json',control.inventory(entry,rt,False)),'source_proof':control.source_proof(rt,out/'runtime-source'),'model_calls':0,'status':'STOPPED_IDENTITIES_AND_SMALL_SOURCE_VERIFIED'};save(out/'receipt.private.json',x)
    elif a.mode=='start':
        need(F['model_admission']=='ROOT_FROZEN','ROOT_FROZEN_RELEASE_REQUIRED');out=D/'runtime-start-r01';out.mkdir();x={'before':save(out/'before.private.json',control.inventory(entry,rt,False))}
        clock={'started_epoch':time.time(),'started_monotonic':time.monotonic(),'limit_seconds':900,'trigger':'BEFORE_FIRST_EXISTING_GPU_SERVICE_START','resets':0};save(D/'gpu-campaign-clock.private.json',clock)
        x['manager_args']=control.manager_args(plan,rt,'start');x['manager_output']=control.command(x['manager_args'],timeout=60).decode('utf-8','replace');x['after']=save(out/'after.private.json',control.inventory(entry,rt,True));x.update(status='EXISTING_GPU2_STARTED_NOT_DOCUMENT_ADMISSION',model_calls=0);save(out/'receipt.private.json',x)
    elif a.mode=='linux-cpu':
        clock=json.loads((D/'gpu-campaign-clock.private.json').read_bytes());need(remaining(clock)>300,'GPU_CAMPAIGN_REMAINING_TOO_SMALL');out=D/'linux-cpu';out.mkdir(exist_ok=True);need(not (out/'receipt.private.json').exists(),'LINUX_CPU_ALREADY_EXECUTED');target=F['remote_worker_directory']
        need(target.startswith('/tmp/trackvance-paddle-diag-') and len(target.rsplit('-',1)[-1])==32,'OWNED_TEMP_PATH')
        create="from pathlib import Path;import os; p=Path("+repr(target)+");p.mkdir(mode=0o700,exist_ok=True);assert not p.is_symlink() and p.lstat().st_uid==os.getuid() and not list(p.iterdir())"
        control.docker('exec',rt['gateway_id'],rt['worker_python'],'-I','-B','-c',create)
        for name,x in [('paddle_service_worker.py',F['worker']),('test_paddle_service_worker.py',F['worker_tests'])]:
            transfer=stage_file(control,rt,x['path'],target+'/'+name,x['sha256']);save(out/(name+'.transfer.private.json'),transfer);need(transfer['exit_code']==0,'OWNED_SOURCE_STAGING_FAILED')
        argv=['docker','--context','default','exec',rt['gateway_id'],rt['worker_python'],'-I','-B',target+'/test_paddle_service_worker.py'];started=time.monotonic()
        result=subprocess.run(argv,capture_output=True,timeout=45,env={k:v for k,v in os.environ.items() if k not in control.OVERRIDES},creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        (out/'stdout.txt').write_bytes(result.stdout);(out/'stderr.txt').write_bytes(result.stderr);text=(result.stdout+result.stderr).decode('utf-8','replace')
        ok=result.returncode==0 and 'Ran 17 tests' in text and '\nOK' in text and 'skipped=' not in text
        x={'status':'PASS' if ok else 'FAIL','tests':17,'skipped':0 if ok else None,'exit_code':result.returncode,'elapsed_seconds':round(time.monotonic()-started,6),'model_calls':0,'worker':F['worker'],'tests_source':F['worker_tests'],'stdout':pin(out/'stdout.txt'),'stderr':pin(out/'stderr.txt')};save(out/'receipt.private.json',x);need(ok,'LINUX_CPU_INSTRUMENTATION_TESTS_FAILED');print(json.dumps({'status':'LINUX_CPU_INSTRUMENTATION_PASS','tests':17,'skipped':0,'model_calls':0}))
    elif a.mode=='run':
        need(F['model_admission']=='ROOT_FROZEN','ROOT_FROZEN_RELEASE_REQUIRED');clock=json.loads((D/'gpu-campaign-clock.private.json').read_bytes());need(remaining(clock)>240,'CAMPAIGN_REMAINING_TOO_SMALL');control.inventory(entry,rt,True);control.quiet_proof(rt);x=run(F,control,D/'execution',clock);print(json.dumps({'status':'DIAGNOSTIC_EXECUTION_CLOSED','document_attempts':x['document_attempts'],'actual_paddle_child_spawns':x['actual_paddle_child_spawns'],'Qwen_calls':0}),flush=True)
    else:
        clock=json.loads((D/'gpu-campaign-clock.private.json').read_bytes());out=D/'runtime-stop-r01';out.mkdir();summary=json.loads((D/'execution/summary.private.json').read_bytes());need(summary['resource_closure_accredited'] is True and summary['admission']=='CLOSED','WORKER_CLOSURE_NOT_ACCREDITED')
        x={'before':save(out/'before.private.json',control.inventory(entry,rt,True)),'quiet_proof':control.quiet_proof(rt),'manager_args':control.manager_args(plan,rt,'stop')};x['manager_output']=control.command(x['manager_args'],timeout=60).decode('utf-8','replace');x['after']=save(out/'after.private.json',control.inventory(entry,rt,False));x['source_proof_after_stop']=control.source_proof(rt,out/'runtime-source-after');x.update(status='SAME31_STOPPED_PID0_METADATA_PRESERVED',GPU_campaign_elapsed_seconds=round(elapsed(clock),6),DB_accesses=0,Qwen_calls=0,model_or_volume_full_content_hash_claim=False);need(elapsed(clock)<=900,'GPU_CAMPAIGN_LIMIT_EXCEEDED');save(out/'receipt.private.json',x);print(json.dumps({'status':x['status'],'GPU_campaign_elapsed_seconds':x['GPU_campaign_elapsed_seconds']}))

if __name__=='__main__':main()
