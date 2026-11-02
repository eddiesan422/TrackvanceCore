"""CPU-only regression tests: original exception/transport semantics and closure."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

import paddle_service_worker as w


class Budget:
    def remaining(self, **kwargs):return 180
    def join_remaining(self):return 210


class Response:
    def __init__(self, status, raw):self.status,self.raw,self.stream=status,raw,io.BytesIO(raw)
    def getheader(self, _name):return 'application/json'
    def read(self, limit):return self.stream.read(limit)
    def readline(self, limit):return self.stream.readline(limit)


class Connection:
    def __init__(self, response):self.response=response;self.sock=None;self.requests=[];self.closed=False
    def request(self,*args,**kwargs):self.requests.append((args,kwargs))
    def getresponse(self):return self.response
    def close(self):self.closed=True


def cpu_request(raw=b'CPU fixture, never sent to a model'):
    h=hashlib.sha256(raw).hexdigest()
    return {'request_id':'a'*32,'source_sha256':h,'source':base64.b64encode(raw).decode(),
            'definition_sha256':'d'*64,'configuration_sha256':'c'*64,'request_budget_seconds':185,
            'configuration':{'extraction_strategy':'PADDLE_STRUCTURED','use_llm':False,
                             'paddle_model':'PaddleOCR-VL-1.6','timeout_seconds':185}}


class ObserverTests(unittest.TestCase):
    def test_safe_error_preserves_exact_return_and_original_error_identity(self):
        events=[];calls=[];observer=w.Observer(lambda kind,**v:events.append((kind,v)))
        answer=(422, 'INFERENCE_FAILED');error=ValueError('private original cause')
        def original(e,i):calls.append((e,i));return answer
        result=observer.safe_error_hook(original)(error,'a'*32)
        self.assertIs(result,answer);self.assertIs(calls[0][0],error)
        self.assertEqual(events[0][0],'EXCEPTION');self.assertIn('ValueError: private original cause',events[0][1]['traceback'])

    def test_observer_emit_failure_does_not_change_error_mapping(self):
        def broken(*args,**kwargs):raise OSError('private diagnostic sink unavailable')
        observer=w.Observer(broken);answer=(504,'INFERENCE_TIMEOUT')
        self.assertIs(observer.safe_error_hook(lambda e,i:answer)(TimeoutError(),None),answer)
        self.assertTrue(observer.observer_errors)

    def test_child_success_identity_and_capture_before_temporary_deletion(self):
        events=[];answer=object()
        with tempfile.TemporaryDirectory(prefix='idp-recognition-') as folder:
            path=Path(folder)
            def original(session,root,operation,consume):
                (root/'error.log').write_bytes(b'complete original stderr')
                return answer
            def capture(root):
                self.assertTrue(root.exists());raw=(root/'error.log').read_bytes()
                return [{'name':'error.log','content_b64':base64.b64encode(raw).decode()}]
            observer=w.Observer(lambda kind,**v:events.append((kind,v)),capture)
            self.assertIs(observer.child_hook(original)(object(),path,'STRUCTURED',None),answer)
        self.assertFalse(path.exists());self.assertEqual(events[0][0],'CAPTURE')
        self.assertEqual(base64.b64decode(events[0][1]['content_b64']),b'complete original stderr')

    def test_child_error_same_object_trace_and_capture_even_when_legacy_ring_rejects(self):
        events=[];error=TypeError('original gateway/child association defect')
        def original(*args):raise error
        observer=w.Observer(lambda kind,**v:events.append((kind,v)),lambda root:[{'name':'error.log','present':True}])
        with self.assertRaises(TypeError) as caught:observer.child_hook(original)(None,Path('/tmp/idp-recognition-test'),'STRUCTURED',None)
        self.assertIs(caught.exception,error);self.assertEqual([x[0] for x in events],['EXCEPTION','CAPTURE'])
        self.assertIn('original gateway/child association defect',events[0][1]['traceback'])

    def test_capture_failure_never_overrides_original_success_or_exception(self):
        def broken_capture(root):raise OSError('capture failed')
        observer=w.Observer(lambda *a,**k:None,broken_capture);answer=object()
        self.assertIs(observer.child_hook(lambda *a:answer)(None,None,None,None),answer)
        error=KeyError('original')
        def original(*args):raise error
        with self.assertRaises(KeyError) as caught:observer.child_hook(original)(None,None,None,None)
        self.assertIs(caught.exception,error);self.assertEqual(len(observer.observer_errors),2)

    def test_popen_only_counts_actual_returned_paddle_process(self):
        events=[];process=types.SimpleNamespace(pid=1234);observer=w.Observer(lambda kind,**v:events.append((kind,v)))
        def popen(*args,**kwargs):return process
        wrapped=observer.popen_hook(popen)
        self.assertIs(wrapped(['/python','-I','/app/paddle_runner.py','/tmp/idp-recognition-x']),process)
        self.assertIs(wrapped(['nvidia-smi']),process)
        self.assertEqual(len(observer.spawned_children),1);self.assertEqual(events[0][0],'CHILD_SPAWN')
        error=OSError('spawn did not happen')
        def failed(*args,**kwargs):raise error
        with self.assertRaises(OSError) as caught:observer.popen_hook(failed)(['/python','/app/paddle_runner.py'])
        self.assertIs(caught.exception,error);self.assertEqual(len(observer.spawned_children),1)


class TransportTests(unittest.TestCase):
    def invoke(self,response):
        connection=Connection(response);events=[]
        result=w.read_recognition(12345,cpu_request(),Budget(),sender=lambda kind,**v:events.append((kind,v)),connection_factory=lambda *a,**k:connection)
        self.assertTrue(connection.closed);self.assertEqual(len(connection.requests),1)
        return result,events

    def test_generic_422_exact_body_retained_without_causal_infrastructure_label(self):
        raw=b'{"error":"INFERENCE_FAILED"}'
        result,events=self.invoke(Response(422,raw))
        self.assertEqual(result['failure_code'],'INFERENCE_FAILED');self.assertEqual(result['status'],'ERROR')
        self.assertEqual(base64.b64decode(next(v['content_b64'] for k,v in events if k=='A_RAW')),raw)
        self.assertNotIn('infrastructure_failure',result)

    def test_timeout_http_is_terminal_timeout_and_body_preserved(self):
        result,_=self.invoke(Response(504,b'{"error":"INFERENCE_TIMEOUT"}'))
        self.assertEqual(result['status'],'TIMEOUT');self.assertEqual(result['failure_code'],'INFERENCE_TIMEOUT')

    def test_page_stream_requires_verified_terminal_and_preserves_raw(self):
        records=[{'kind':'PAGE','payload':{'opaque':'unchanged'}},{'kind':'TERMINAL','status':'COMPLETE','resources_released':True,'payload':{}}]
        raw=b''.join(w.canonical(x)+b'\n' for x in records)
        result,events=self.invoke(Response(200,raw));self.assertEqual(result['status'],'COMPLETE');self.assertEqual(result['page_events'],1)
        self.assertEqual(b''.join(base64.b64decode(v['content_b64']) for k,v in events if k=='A_RAW'),raw)
        connection=Connection(Response(200,b'{"kind":"PAGE"}\n'))
        with self.assertRaisesRegex(ValueError,'PADDLE_TERMINAL_CLOSE_UNVERIFIED'):
            w.read_recognition(12345,cpu_request(),Budget(),sender=lambda *a,**k:None,connection_factory=lambda *a,**k:connection)
        self.assertTrue(connection.closed)

    def test_adjusted_budget_preserves_request_and_original_reserve_formula(self):
        request=cpu_request();actual=w.adjusted_request(request,179)
        self.assertEqual(actual['request_budget_seconds'],184);self.assertEqual(request['request_budget_seconds'],185)
        self.assertEqual({k:v for k,v in actual.items() if k!='request_budget_seconds'},{k:v for k,v in request.items() if k!='request_budget_seconds'})


class AdmissionAndClosureTests(unittest.TestCase):
    def gate(self):
        r=cpu_request();return w.AttemptGate({'allowed_documents':['F02-02','F01-02'],'document_sha256':{'F02-02':r['source_sha256'],'F01-02':r['source_sha256']}})
    def command(self,identity='F02-02'):
        return {'kind':'RUN','document_id':identity,'a_request':cpu_request(),'work_seconds':180}

    def test_only_fixed_order_hash_and_one_admission_with_uncertain_close_block(self):
        gate=self.gate()
        with self.assertRaisesRegex(ValueError,'FIXED_DOCUMENT_ORDER'):gate.admit(self.command('F01-02'))
        gate.admit(self.command());self.assertEqual(gate.consumed,['F02-02'])
        with self.assertRaisesRegex(ValueError,'PREVIOUS_CLOSURE_UNCONFIRMED'):gate.admit(self.command('F01-02'))
        gate.closed=True
        with self.assertRaisesRegex(ValueError,'DOCUMENT_ALREADY_CONSUMED'):gate.admit(self.command())
        gate.admit(self.command('F01-02'));self.assertEqual(gate.consumed,['F02-02','F01-02'])

    def test_no_qwen_or_changed_source_configuration_can_be_admitted(self):
        for key,value in [('extraction_strategy','QWEN_VISUAL'),('use_llm',True),('timeout_seconds',300)]:
            gate=self.gate();command=self.command();command['a_request']['configuration'][key]=value
            with self.assertRaisesRegex(ValueError,'FIXED_PADDLE_ONLY_CONFIGURATION'):gate.admit(command)
            self.assertEqual(gate.consumed,[])
        gate=self.gate();command=self.command();command['a_request']['source']=base64.b64encode(b'other bytes').decode()
        with self.assertRaisesRegex(ValueError,'SOURCE_BYTES_CHANGED'):gate.admit(command)

    def test_active_process_or_unjoined_http_blocks_close_immediately(self):
        gateway=types.SimpleNamespace(ACTIVE={'id':object()},UPSTREAM={})
        with self.assertRaisesRegex(ValueError,'DIAGNOSTIC_PROCESS_OR_READER_NOT_CLOSED'):
            w.close_proof(gateway,'id',Budget(),{})
        server=types.SimpleNamespace(join_handlers=lambda timeout:False)
        with self.assertRaisesRegex(ValueError,'DIAGNOSTIC_HTTP_HANDLER_NOT_JOINED'):
            w.close_proof(gateway,'id',Budget(),{},server)

    def test_closed_process_requires_empty_residency_and_health_not_inactive_gpu_assumption(self):
        expected={'server.py':'a'*64};health={'ready':True,'busy':False,'memory_lifecycle':{'status':'AVAILABLE'},'gpu_concurrency':1,'runtime_provenance':{'gateway':{'files_sha256':expected}}}
        gateway=types.SimpleNamespace(ACTIVE={},UPSTREAM={},health=lambda:health,
             memory_barrier=lambda *a,**k:{'status':'AVAILABLE'},memory_request=lambda *a,**k:{'models':[]})
        server=types.SimpleNamespace(join_handlers=lambda timeout:True)
        with patch.object(w,'child_pids',return_value=[]),patch.object(w,'compute_processes',return_value=''),patch.object(w,'main_health',return_value=health):
            proof=w.close_proof(gateway,'id',Budget(),expected,server)
            self.assertTrue(proof['resources_released']);self.assertTrue(proof['diagnostic_HTTP_handlers_joined'])
            gateway.memory_request=lambda *a,**k:{'models':[{'name':'foreign'}]}
            with self.assertRaisesRegex(ValueError,'OLLAMA_RESIDENCY_NOT_EMPTY'):w.close_proof(gateway,'id',Budget(),expected,server)

    def test_real_cpu_loopback_handler_threads_join(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):self.send_response(200);self.end_headers();self.wfile.write(b'CPU fixture')
            def log_message(self,*args):pass
        server=w.DiagnosticHTTPServer(('127.0.0.1',0),Handler)
        serving=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});serving.start()
        connection=w.http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=2)
        try:
            connection.request('GET','/');response=connection.getresponse();self.assertEqual(response.read(),b'CPU fixture');connection.close()
            self.assertTrue(server.join_handlers(2))
        finally:
            connection.close();server.shutdown();server.server_close();serving.join(2)
        self.assertFalse(serving.is_alive())


@unittest.skipUnless(hasattr(os,'O_DIRECTORY') and hasattr(os,'O_NOFOLLOW') and hasattr(os,'getuid'),'POSIX directory-FD security requires Linux')
class PosixCaptureTests(unittest.TestCase):
    def test_full_bytes_and_missing_metadata_are_explicit(self):
        with tempfile.TemporaryDirectory() as base:
            folder=Path(base)/'idp-recognition-cpu';folder.mkdir();(folder/'error.log').write_bytes(b'complete\x00original\n')
            records=w.capture_job(folder,base=Path(base))
            record=next(r for r in records if r['name']=='error.log');self.assertFalse(record['truncated'])
            self.assertEqual(base64.b64decode(record['content_b64']),b'complete\x00original\n')
            self.assertTrue(all(not r['present'] for r in records if r['name']!='error.log'))

    def test_symlink_and_size_limit_are_rejected(self):
        with tempfile.TemporaryDirectory() as base:
            root=Path(base);folder=root/'idp-recognition-cpu';folder.mkdir();outside=root/'outside';outside.write_bytes(b'private')
            (folder/'error.log').symlink_to(outside)
            with self.assertRaises(OSError):w.capture_job(folder,base=root)
            (folder/'error.log').unlink();(folder/'error.log').write_bytes(b'12345')
            with self.assertRaisesRegex(ValueError,'CAPTURE_FILE_LIMIT'):w.capture_job(folder,base=root,limit=4)


if __name__=='__main__':unittest.main(verbosity=2)
