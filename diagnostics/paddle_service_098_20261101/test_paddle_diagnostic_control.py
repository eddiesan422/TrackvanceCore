"""Host admission-boundary checks; no subprocess, Docker or model execution."""
import importlib.util,sys,unittest,json,queue,io,tempfile,time
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('paddle_host_control_tested',Path(__file__).with_name('paddle_diagnostic_control.py'))
m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
class HostAdmission(unittest.TestCase):
    def test_host_worker_protocol_and_cleanup_compile_without_dispatching_GPU(self):
        # Exercise the actual host parser/handshake against synthetic transport,
        # including WORKER_CLOSED and the exact generated cleanup Python source.
        class Output:
            def __init__(self):self.lines=queue.Queue()
            def readline(self,_limit):return self.lines.get(timeout=3)
            def add(self,x):self.lines.put(json.dumps(x).encode()+b'\n')
        class Process:
            def __init__(self):
                self.stdout=Output();self.stderr=io.BytesIO();self.stdin=self;self.commands=[]
            def write(self,raw):
                x=json.loads(raw);self.commands.append(x)
                if 'allowed_documents' in x:self.stdout.add({'kind':'READY'})
                elif x.get('kind')=='RUN':
                    self.stdout.add({'kind':'CHILD_SPAWN','pid':123})
                    self.stdout.add({'kind':'RESULT','result':{'status':'COMPLETE','resources_released':True,'http_status':200}})
                elif x.get('kind')=='CLOSE':
                    self.stdout.add({'kind':'WORKER_CLOSED','effective_close':True,'owned_temporaries':[]});self.stdout.lines.put(b'')
                else:raise AssertionError('INCOMPATIBLE_HOST_CONTROL_PROTOCOL')
            def flush(self):pass
            def close(self):self.stdout.lines.put(b'')
            def wait(self,timeout):return 0
        process=Process()
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp);(d/'linux-cpu').mkdir();(d/'linux-cpu/receipt.private.json').write_text(json.dumps({'status':'PASS','tests':17,'skipped':0}))
            source=d/'synthetic.pdf';source.write_bytes(b'unit-fixture-no-model')
            docs=[]
            for i in ['F02-02','F01-02']:
                docs.append({'document_id':i,'source':m.pin(source),'configuration':{},'configuration_sha256':'0'*64,'definition':{},'definition_sha256':'0'*64,'request_id':'1'*32,'attempt_id':'cpu-fixture'})
            dp=d/'documents.json';dp.write_text(json.dumps({'documents':docs}))
            f={'directory':str(d),'runtime':{'gateway_id':'synthetic-only','worker_python':'python','startup_files_sha256':{}},'policy':{'path':str(Path(__file__).with_name('paddle_failure_policy.py'))},'documents':m.pin(dp),'remote_worker_directory':'/tmp/trackvance-paddle-diag-'+'a'*32,'worker':{'sha256':'b'*64},'worker_tests':{'sha256':'c'*64}}
            class Control:
                OVERRIDES=set()
                def docker(self,*args):
                    code=args[-1]
                    if 'OWNED_WORKER_TEMP_AND_LOCK_REMOVED' in code:
                        compile(code,'cpu-checked-cleanup','exec');return b'OWNED_WORKER_TEMP_AND_LOCK_REMOVED'
                    return (f['worker']['sha256']+'\n').encode()
            clock={'started_monotonic':time.monotonic(),'started_epoch':time.time()}
            with patch.object(m.subprocess,'Popen',return_value=process):
                result=m.run(f,Control(),d/'execution',clock)
            self.assertEqual(result['document_attempts'],1)
            self.assertEqual(result['actual_paddle_child_spawns'],1)
            self.assertEqual(result['rows'][1]['status'],'NOT_RUN_NOT_NECESSARY')
            self.assertEqual([x.get('kind') for x in process.commands],[None,'RUN','CLOSE'])
    def test_success_never_hunts_for_failure(self):
        self.assertFalse(m.control_necessity({'status':'COMPLETE','resources_released':True},1)[0])
    def test_pre_child_gateway_failure_needs_trace_not_second_document(self):
        self.assertFalse(m.control_necessity({'status':'ERROR','resources_released':True},0)[0])
    def test_child_failure_or_timeout_can_use_one_control_after_closure(self):
        for status in ['ERROR','TIMEOUT']:
            self.assertTrue(m.control_necessity({'status':status,'resources_released':True},1)[0])
    def test_no_control_if_effective_closure_unknown_even_after_success(self):
        for closure in [None,False,1,'true']:
            with self.assertRaisesRegex(RuntimeError,'CLOSURE_UNCONFIRMED'):
                m.control_necessity({'status':'COMPLETE','resources_released':closure},1)
    def test_campaign_uses_existing_clock_not_a_new_document_budget(self):
        clock={'started_monotonic':100.,'started_epoch':1000.}
        with patch.object(m.time,'monotonic',return_value=750.),patch.object(m.time,'time',return_value=1650.):
            self.assertEqual(m.remaining(clock),250.)
        self.assertEqual(clock,{'started_monotonic':100.,'started_epoch':1000.})
    def test_clock_discontinuity_refuses_new_admission(self):
        with patch.object(m.time,'monotonic',return_value=110.),patch.object(m.time,'time',return_value=1100.):
            with self.assertRaisesRegex(RuntimeError,'CLOCK_DISCONTINUITY'):
                m.remaining({'started_monotonic':100.,'started_epoch':1000.})
if __name__=='__main__':unittest.main(verbosity=2)
