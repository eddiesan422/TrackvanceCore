#!/usr/bin/env python3
"""Bounded 0.7.0 volume certification in an explicitly guarded disposable stack.

No stack is created, started or deleted. --prepare-only performs host fixture work
only. The existing certification context owns every SQL fixture and restart.
Reports record actual outcomes; an unexecuted tier never becomes PASS.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import http.client
import io
import json
import math
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zlib
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import certification_v070 as certification
from smoke_test import Api

MIB, GIB = 1024**2, 1024**3
SEED = b'trackvance-v070-million-observed-v1'
COLUMNS = ('record_id', 'amount', 'region', 'observed', 'payload')
TERMINAL = {'SUCCESS', 'FAILED', 'CANCELLED', 'FAILED_PRECONDITION', 'UNKNOWN'}
ACTIVE_MEASUREMENTS = None
RESOURCE_MEMORY_BUDGET = 4 * GIB
OWNED_OPERATIONS = []


def canonical_bytes(row):
    return json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'


def row_for(number, width, mode='varied'):
    raw = hashlib.shake_256(SEED + number.to_bytes(8, 'big')).digest(math.ceil(width * 3 / 4) + 3)
    payload = base64.urlsafe_b64encode(raw).decode('ascii')[:width] if mode == 'varied' else 'x' * width
    label = None if number % 100 == 0 else '' if number % 100 == 1 else (
        f'  é-{number:012d}-😀\ncontinuación  ' if number % 10000 == 2 else f'  é-{number:012d}-😀  ')
    return dict(zip(COLUMNS, (f'{number:012d}', f'{number % 997 + 1}.00000001', f'R{number % 20:02d}', label, payload), strict=True))


def generate(directory, size_mib, rows=1_000_000, mode='varied'):
    import pyarrow as pa
    import pyarrow.parquet as pq
    directory.mkdir(parents=True, exist_ok=True)
    basename = f'volume-{size_mib}mib-{rows}-{mode}'
    paths = {kind: directory / f'{basename}.{extension}' for kind, extension in
             [('CSV', 'csv'), ('JSONL', 'ndjson'), ('PARQUET', 'parquet')]}
    metadata = directory / f'{basename}.json'
    if metadata.exists():
        cached = json.loads(metadata.read_text(encoding='utf-8'))
        for kind, path in paths.items():
            with path.open('rb') as source:
                if hashlib.file_digest(source, 'sha256').hexdigest() != cached['formats'][kind]['sha256']:
                    raise RuntimeError('Un fixture cacheado cambió; no se reutiliza evidencia inconsistente.')
        return cached
    target = size_mib * MIB
    width = max(32, math.ceil(target / rows) - 40)
    if width > 60000 or not 1 <= rows <= 100_000_000:
        raise ValueError('El fixture excede el límite explícito por celda o población.')
    schema = pa.schema([(column, pa.string()) for column in COLUMNS])
    hashes = {kind: hashlib.sha256() for kind in ('CSV', 'JSONL')}
    semantic, compressed, compressor = hashlib.sha256(), 0, zlib.compressobj(level=6, wbits=31)
    observed_bytes = minimum = maximum = 0
    batch = []
    started = time.monotonic()
    with paths['CSV'].open('xb') as output, paths['JSONL'].open('xb') as jsonl, pq.ParquetWriter(paths['PARQUET'], schema, compression='zstd') as parquet:
        header = (','.join(COLUMNS) + '\n').encode()
        output.write(header); hashes['CSV'].update(header)
        compressed += len(compressor.compress(header))
        for number in range(1, rows + 1):
            row = row_for(number, width, mode)
            # csv.writer quotes empty observed text; None remains unquoted null.
            fields = [row[column] for column in COLUMNS]
            rendered = io.StringIO(newline='')
            csv.writer(rendered, lineterminator='\n').writerow(fields)
            line = rendered.getvalue()
            if row['observed'] == '':
                # Five columns with observed at ordinal3; csv.writer does not
                # quote a non-single empty field, so preserve its exact contract.
                pieces = line.split(',')
                pieces[3] = '""'
                line = ','.join(pieces)
            encoded, canonical = line.encode('utf-8'), canonical_bytes(row)
            output.write(encoded); jsonl.write(canonical)
            hashes['CSV'].update(encoded); hashes['JSONL'].update(canonical); semantic.update(canonical)
            compressed += len(compressor.compress(encoded))
            length = sum(len(value.encode('utf-8')) for value in fields if value is not None)
            observed_bytes += length
            minimum = length if number == 1 else min(minimum, length)
            maximum = max(maximum, length)
            batch.append(row)
            if len(batch) == 2048:
                parquet.write_table(pa.Table.from_pylist(batch, schema=schema), row_group_size=2048)
                batch.clear()
        if batch:
            parquet.write_table(pa.Table.from_pylist(batch, schema=schema), row_group_size=2048)
    compressed += len(compressor.flush())
    with paths['PARQUET'].open('rb') as source:
        parquet_hash = hashlib.file_digest(source, 'sha256').hexdigest()
    formats = {kind: {'path': str(path.resolve()), 'actual_bytes': path.stat().st_size,
               'sha256': parquet_hash if kind == 'PARQUET' else hashes[kind].hexdigest()}
               for kind, path in paths.items()}
    outcome = {'target_mib': size_mib, 'rows': rows, 'columns': list(COLUMNS), 'mode': mode,
        'seed_sha256': hashlib.sha256(SEED).hexdigest(), 'payload_method': 'SHAKE256_URLSAFE_V1' if mode == 'varied' else 'REPEATED_X_V1',
        'payload_width_bytes': width, 'observed_utf8_bytes': observed_bytes,
        'row_width_bytes': {'min': minimum, 'max': maximum, 'mean': observed_bytes / rows},
        'expected_cardinality': {'record_id': rows, 'payload': rows if mode == 'varied' else 1, 'region': min(rows, 20)},
        'expected_observed_nulls': rows // 100, 'expected_observed_empty': len(range(1, rows + 1, 100)),
        'canonical_rows_sha256': semantic.hexdigest(), 'formats': formats,
        'csv_gzip_size_bytes': compressed, 'csv_gzip_ratio': compressed / formats['CSV']['actual_bytes'],
        'generation_seconds': round(time.monotonic() - started, 3), 'generation_max_batch_rows': 2048}
    metadata.write_text(json.dumps(outcome, ensure_ascii=False, indent=2), encoding='utf-8')
    return outcome


class VolumeApi(Api):
    def binary(self, path):
        endpoint = urlsplit(self.base_url)
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=self.timeout)
        try:
            headers = {'Content-Type': 'application/octet-stream', 'Content-Length': str(path.stat().st_size),
                'X-CSRF-Token': self.csrf, 'Cookie': '; '.join(f'{cookie.name}={cookie.value}' for cookie in self.cookies)}
            connection.putrequest('POST', '/api/v1/datasets/uploads/stage?filename=' + quote(path.name))
            for key, value in headers.items():
                connection.putheader(key, value)
            connection.endheaders()
            with path.open('rb') as source:
                while chunk := source.read(1024 * 1024):
                    connection.send(chunk)
            response = connection.getresponse()
            encoded = response.read()
            try: body = json.loads(encoded)
            except (ValueError, UnicodeError):
                raise RuntimeError(f'Recepción rechazó HTTP{response.status} antes del contrato JSON de la API.') from None
            if response.status != 201:
                raise RuntimeError(f'Recepción falló HTTP{response.status}: {body.get("error", {}).get("code", "UNKNOWN")}')
            return body
        finally:
            connection.close()

    def keyed(self, path, body, key):
        request = urllib.request.Request(self.base_url + '/api/v1' + path,
            data=json.dumps(body).encode(), headers={'Content-Type': 'application/json',
                'X-CSRF-Token': self.csrf, 'Idempotency-Key': key}, method='POST')
        with self.opener.open(request, timeout=self.timeout) as response:
            if response.status != 202:
                raise RuntimeError('Una operación durable no devolvió202.')
            return json.load(response)


def wait(api, path, timeout=1800, predicate=None):
    started, last = time.monotonic(), None
    while time.monotonic() - started < timeout:
        if ACTIVE_MEASUREMENTS is not None: ACTIVE_MEASUREMENTS.check()
        current = api.get('/api/v1' + path)
        if predicate(current) if predicate else current.get('status') in TERMINAL:
            return current
        state = (current.get('status'), current.get('stage'), current.get('processed_rows'))
        if state != last:
            print(json.dumps({'path': path, 'status': state[0], 'stage': state[1], 'processed_rows': state[2]}), flush=True)
            last = state
        time.sleep(1)
    raise TimeoutError('La fase excedió su timeout de certificación.')


PROBE = """import json, pathlib, os
rss=0
for p in pathlib.Path('/proc').glob('[0-9]*/stat'):
 try: rss+=int(p.read_text().rsplit(')',1)[1].split()[21])*os.sysconf('SC_PAGE_SIZE')
 except (OSError, ValueError, IndexError): pass
root=pathlib.Path('/sys/fs/cgroup')
result={'process_sum_rss_bytes':rss}
for key in ['memory.current','memory.peak']:
 try: result[key]=int((root/key).read_text())
 except (OSError,ValueError): pass
try: result['cpu']=dict(line.split() for line in (root/'cpu.stat').read_text().splitlines())
except OSError: pass
try: result['memory_events']=dict(line.split() for line in (root/'memory.events').read_text().splitlines())
except OSError: pass
from trackvance.config import STORAGE_DIR
result['temporary_bytes']=sum(p.stat().st_size for p in (STORAGE_DIR/'tmp').rglob('*') if p.is_file()) if (STORAGE_DIR/'tmp').exists() else 0
print(json.dumps(result))
"""


class Measurements:
    def __init__(self, directory, context):
        self.directory, self.context = directory, context
        self.stop, self.samples, self.errors = threading.Event(), [], []
        self.violation = None
        self.worker = threading.Thread(target=self.sample, daemon=True)
    def sample(self):
        while not self.stop.is_set():
            try:
                resources = certification.inventory(self.context['project'])
                current = {'at': time.time(), 'disk_free_bytes': shutil.disk_usage(ROOT).free, 'services': {}}
                for item in resources:
                    if item['state'] == 'running' and item['service'] in {'api', 'worker', 'acquisition-worker', 'delivery-worker'}:
                        result = subprocess.run(['docker', 'exec', item['id'], 'python', '-c', PROBE], capture_output=True, text=True, timeout=10, check=False)
                        if result.returncode == 0:
                            current['services'][item['service']] = json.loads(result.stdout)
                            current['services'][item['service']]['container_id'] = item['id']
                self.samples.append(current)
                if current['disk_free_bytes'] < 5 * GIB:
                    self.violation = 'RESOURCE_DISK_RESERVE: host libre inferior a 5GiB.'
                if sum(values.get('memory.current',0) for values in current['services'].values()) > RESOURCE_MEMORY_BUDGET:
                    self.violation = 'RESOURCE_MEMORY_RESERVE: cgroups backend superan el presupuesto reservado.'
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                self.errors.append('METRICS_SAMPLE_UNAVAILABLE')
            self.stop.wait(2)
    def __enter__(self):
        self.started = time.monotonic(); self.worker.start(); return self
    def __exit__(self, *_):
        self.stop.set(); self.worker.join(timeout=45)
    def check(self):
        if self.violation: raise RuntimeError(self.violation)
    def report(self):
        services = {}
        for sample in self.samples:
            for service, values in sample['services'].items():
                record = services.setdefault(service, {'rss_peak_bytes': 0, 'cgroup_sample_peak_bytes': 0, 'cgroup_lifetime_peak_bytes': 0, 'temporary_peak_bytes': 0})
                for target, source in [('rss_peak_bytes','process_sum_rss_bytes'), ('cgroup_sample_peak_bytes','memory.current'), ('cgroup_lifetime_peak_bytes','memory.peak'), ('temporary_peak_bytes','temporary_bytes')]:
                    record[target] = max(record[target], values.get(source, 0))
                previous = record.get('_previous')
                record.setdefault('cpu_seconds', 0.0)
                record.setdefault('cpu_counter_resets', 0)
                record.setdefault('memory_events_delta', {})
                if previous is not None:
                    before = int(previous.get('cpu', {}).get('usage_usec', 0))
                    after = int(values.get('cpu', {}).get('usage_usec', 0))
                    if values.get('container_id') == previous.get('container_id') and after >= before:
                        record['cpu_seconds'] += (after - before) / 1_000_000
                        first, last = previous.get('memory_events', {}), values.get('memory_events', {})
                        for key in set(first) | set(last):
                            record['memory_events_delta'][key] = record['memory_events_delta'].get(key, 0) + max(0, int(last.get(key, 0)) - int(first.get(key, 0)))
                    else:
                        record['cpu_counter_resets'] += 1
                record['_previous'] = values
        for record in services.values():
            record.pop('_previous')
            record['cpu_measurement_complete'] = record['cpu_counter_resets'] == 0
            record['memory_events_measurement_complete'] = record['cpu_counter_resets'] == 0
            record['cpu_seconds'] = round(record['cpu_seconds'], 6)
        return {'elapsed_seconds': round(time.monotonic()-self.started, 3), 'samples': len(self.samples), 'services': services,
            'combined_cgroup_sample_peak_bytes': max((sum(values.get('memory.current',0) for values in sample['services'].values()) for sample in self.samples), default=None),
            'minimum_host_disk_free_bytes': min((sample['disk_free_bytes'] for sample in self.samples), default=None),
            'measurement_errors': self.errors, 'method': 'CONTAINER_PROC_RSS_SUM_CGROUP_V2_CPU_STAT_DISK_2S',
            'resource_guard':self.violation,'backend_cgroup_budget_bytes':RESOURCE_MEMORY_BUDGET,'host_disk_reserve_bytes':5*GIB,
            'cgroup_lifetime_peak_is_not_phase_peak': True}


@contextmanager
def phase(report, label, directory, context):
    global ACTIVE_MEASUREMENTS
    print('Fase: ' + label, flush=True)
    metrics = Measurements(directory, context)
    try:
        with metrics:
            ACTIVE_MEASUREMENTS = metrics
            yield
    finally:
        report.setdefault('phases', {})[label] = metrics.report()
        ACTIVE_MEASUREMENTS = None
    # Hash verification and SQL COPY can finish without another polling call.
    # A reserve violated there still fails this phase instead of being ignored.
    metrics.check()


def psql(directory, context, sql, *, input_path=None):
    command = ['docker','compose','--project-name',context['project'],'--env-file',str(directory/'test.env'),
        '-f',str(ROOT/'compose.yml'),'-f',str(directory/'compose.json'),'exec','-T','postgres',
        'psql','-U','tv_v070_test','-d','tv_v070_test','-v','ON_ERROR_STOP=1','-t','-A']
    if input_path:
        command += ['-c', sql]
        with input_path.open('rb') as stream:
            result = subprocess.run(command, stdin=stream, capture_output=True, timeout=1800, check=False)
    else:
        result = subprocess.run(command, input=sql.encode(), capture_output=True, timeout=1800, check=False)
    if result.returncode:
        raise RuntimeError('La fixture SQL del proyecto de certificación falló; la salida privada no se publica.')
    return result.stdout.decode('utf-8')


def target_hash(directory, context, schema, table):
    """Stream every committed target row; JSON preserves NULL/empty/Unicode.

    NUMERIC::text retains the declared eight decimal places. COPY CSV protects
    JSON backslashes without the escaping of the default COPY text protocol.
    """
    projection = ','.join("'"+name+"',"+name+('::text' if name=='amount' else '') for name in COLUMNS)
    query = f"COPY (SELECT json_build_object({projection})::text FROM {schema}.{table} ORDER BY record_id) TO STDOUT WITH (FORMAT CSV);"
    command = ['docker','compose','--project-name',context['project'],'--env-file',str(directory/'test.env'),
        '-f',str(ROOT/'compose.yml'),'-f',str(directory/'compose.json'),'exec','-T','postgres',
        'psql','-U','tv_v070_test','-d','tv_v070_test','-v','ON_ERROR_STOP=1','-t','-A','-c',query]
    digest, count = hashlib.sha256(), 0
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True, encoding='utf-8', bufsize=1024*1024) as process:
        assert process.stdout is not None
        for encoded in csv.reader(process.stdout):
            digest.update(canonical_bytes(json.loads(encoded[0]))); count += 1
        if process.wait(timeout=60):
            raise RuntimeError('No fue posible verificar todas las filas comprometidas del destino.')
    return {'rows':count, 'canonical_rows_sha256':digest.hexdigest(), 'max_buffer_rows':1}


def materialized_hash(directory, context, version_id):
    script = """import json,hashlib
from sqlalchemy import select
from trackvance.db import SessionLocal
from trackvance.models import DatasetVersion
from trackvance.dataset_scans import version_paths,bounded_scan
identifier=__VERSION__
columns=__COLUMNS__
digest=hashlib.sha256(); count=0; nulls=0; empty=0
with SessionLocal() as db:
 version=db.get(DatasetVersion,identifier)
 with bounded_scan(version_paths(db,version)) as connection:
  cursor=connection.execute('SELECT '+','.join('"'+c+'"' for c in columns)+' FROM population ORDER BY "record_id"')
  while rows:=cursor.fetchmany(128):
   for row in rows:
    digest.update((json.dumps(dict(zip(columns,row)),sort_keys=True,ensure_ascii=False,separators=(',',':'))+'\\n').encode()); count+=1
    nulls+=row[3] is None; empty+=row[3]==''
print(json.dumps({'rows':count,'canonical_rows_sha256':digest.hexdigest(),'observed_null_count':nulls,'observed_empty_string_count':empty}))
""".replace('__VERSION__', repr(version_id)).replace('__COLUMNS__', repr(list(COLUMNS)))
    return json.loads(certification.compose(directory, context, ['exec','-T','api','python','-c',script]))


def verify_browser_report(directory, context, report_path):
    """Verify all browser-created source, accepted and committed SQL values."""
    observed = json.loads(report_path.read_text(encoding='utf-8'))
    if (observed.get('project') != context['project'] or observed.get('expected_rows') != 1_000_000
            or not re.fullmatch(r'volume_[a-f0-9]+', str(observed.get('schema', '')))
            or not re.fullmatch(r'ui_volume_\d+', str(observed.get('table', '')))
            or not re.fullmatch(r'[a-f0-9]{64}', str(observed.get('expected_canonical_rows_sha256', '')))):
        raise ValueError('El informe del navegador no pertenece al fixture y proyecto protegidos.')
    for key in ('acquisition_id', 'source_version_id', 'intake_run_id', 'output_version_id', 'delivery_run_id', 'automation_id'):
        UUID(observed[key])
    certification.assert_main_unchanged(context)
    verified = {'schema_version': 1, 'project': context['project'], 'status': 'PASS',
                'source': materialized_hash(directory, context, observed['source_version_id']),
                'accepted': materialized_hash(directory, context, observed['output_version_id']),
                'target': target_hash(directory, context, observed['schema'], observed['table'])}
    for key in ('source', 'accepted', 'target'):
        if (verified[key]['rows'] != observed['expected_rows']
                or verified[key]['canonical_rows_sha256'] != observed['expected_canonical_rows_sha256']):
            raise RuntimeError('El flujo de navegador no conservó todas las filas y valores del fixture.')
    (directory / 'browser-volume-integrity.json').write_text(json.dumps(verified, indent=2), encoding='utf-8')
    certification.assert_main_unchanged(context)
    return verified


def cleanup_corrupt_staging(directory, context, acquisition_id):
    """Retire only the deliberately corrupted, terminal private fixture upload."""
    UUID(acquisition_id)
    script = """import json
from datetime import timedelta
from sqlalchemy import select
from trackvance.db import SessionLocal
from trackvance.models import Artifact, Job, utcnow
from trackvance.acquisition_models import AcquisitionRun, AcquisitionUpload
from trackvance.artifactstore import artifact_store
with SessionLocal() as db:
 run=db.scalar(select(AcquisitionRun).where(AcquisitionRun.id==__RUN__).with_for_update())
 assert run.status=='FAILED' and run.error_code=='ARTIFACT_INTEGRITY_ERROR' and run.output_version_id is None
 job=db.scalar(select(Job).where(Job.acquisition_id==run.id).with_for_update())
 assert job.status=='FAILED'
 upload=db.scalar(select(AcquisitionUpload).where(AcquisitionUpload.id==run.upload_id).with_for_update())
 assert upload.status in {'REGISTERED','EXPIRED'}
 path=artifact_store.checked_path(upload.path)
 assert path.is_relative_to(artifact_store.location('tmp'))
 assert db.scalar(select(Artifact.id).where(Artifact.path==str(path)).limit(1)) is None
 existed=path.exists()
 path.unlink(missing_ok=True)
 upload.status='EXPIRED'; upload.expires_at=utcnow()-timedelta(seconds=1)
 db.commit()
 print(json.dumps({'upload_status':'EXPIRED','removed_private_file':existed,'immutable_artifacts_modified':0}))
""".replace('__RUN__', repr(acquisition_id))
    certification.assert_main_unchanged(context)
    result = json.loads(certification.compose(directory, context, ['exec', '-T', 'api', 'python', '-c', script]))
    certification.assert_main_unchanged(context)
    return result


def acquire(api, path, name):
    transferred = api.binary(path)
    dataset = api.post('/api/v1/datasets', {'name':name})
    key = 'volume-' + uuid4().hex
    run = api.keyed(f'/datasets/{dataset["id"]}/acquisitions', {'upload_id':transferred['upload']['id']}, key)
    OWNED_OPERATIONS.append('/acquisitions/'+run['id'])
    repeated = api.keyed(f'/datasets/{dataset["id"]}/acquisitions', {'upload_id':transferred['upload']['id']}, key)
    if run['id'] != repeated['id'] or run['output_version_id'] is not None:
        raise RuntimeError('Registro202 no fue idempotente o publicó dentro de la petición.')
    completed = wait(api, '/acquisitions/' + run['id'])
    if completed['status'] != 'SUCCESS':
        raise RuntimeError('Adquisición fallida: ' + str(completed.get('error_code')))
    return dataset, completed


def validate_profile(profile, fixture):
    """Compare persisted full-population profile counters with fixture truth."""
    persisted = profile['profile']
    columns = {column['name']: column for column in persisted['columns']}
    if persisted['row_count'] != fixture['rows']:
        raise RuntimeError('El perfil global contiene una población diferente.')
    for name, expected in fixture['expected_cardinality'].items():
        if columns[name]['distinct_count'] != expected:
            raise RuntimeError('La cardinalidad global no coincide con el fixture completo.')
    if columns['observed']['null_count'] != fixture['expected_observed_nulls']:
        raise RuntimeError('El perfil confundió texto vacío y valores nulos.')
    return {'row_count': persisted['row_count'],
            'columns': [{key: column.get(key) for key in ('name', 'logical_type', 'distinct_count', 'null_count', 'empty_string_count', 'min', 'max', 'sum', 'max_length')}
                        for column in persisted['columns']],
            'observed_record_bytes_upper_bound': persisted.get('observed_record_bytes_upper_bound')}


def destination_fixture(api, directory, context, token):
    reader, writer = 'vr_' + token, 'vw_' + token
    schema, password = 'volume_' + token, secrets.token_urlsafe(32)
    psql(directory, context, f"CREATE ROLE {reader} LOGIN PASSWORD '{password}'; CREATE ROLE {writer} LOGIN PASSWORD '{password}'; "
        f'CREATE SCHEMA {schema}; GRANT USAGE ON SCHEMA {schema} TO {reader}; GRANT USAGE, CREATE ON SCHEMA {schema} TO {writer};')
    destination = api.post('/api/v1/delivery/destinations', {'name':'Volume writer '+token, 'sink_type':'POSTGRESQL',
        'host':'postgres','port':5432,'database':'tv_v070_test','username':writer,'password':password,'options':{'sslmode':'disable','query_timeout':60}})
    connection = api.post('/api/v1/connections', {'name':'Volume reader '+token, 'source_type':'POSTGRESQL',
        'host':'postgres','port':5432,'database':'tv_v070_test','username':reader,'password':password,'options':{'sslmode':'disable','query_timeout':60}})
    return destination, connection, schema, reader


def chain(api, directory, context, fixture, dataset, source, destination, schema, report):
    version_id = source['output_version_id']
    contract = api.post('/api/v1/intake/contracts', {'name':'Volume exact '+uuid4().hex[:8], 'dataset_id':dataset['id'],
        'config':{'required_columns':['record_id'], 'positive_columns':['amount'], 'max_error_rate':0}})
    draft = {'dataset_version_id':version_id,'destination_id':destination['id'],'destination_version_id':destination['destination_version_id'],
        'target':{'mode':'CREATE_TABLE','schema_name':schema,'table_name':'accepted_'+uuid4().hex[:8],'create_schema':False},
        'columns':[{'source_name':column,'target_name':column,'target_type':'DECIMAL' if column=='amount' else 'STRING',
            'ordinal':index,'nullable':column=='observed', **({'precision':24,'scale':8} if column=='amount' else {})}
            for index,column in enumerate(COLUMNS)], 'write_strategy':'CREATE_AND_LOAD'}
    with phase(report,'delivery_complete_preflight',directory,context):
        validation = api.post('/api/v1/delivery/validations',draft,expected=(202,))
        OWNED_OPERATIONS.append('/delivery/validations/'+validation['id'])
        validated = wait(api,'/delivery/validations/'+validation['id'])
        if validated['status']!='SUCCESS': raise RuntimeError('Preflight completo falló.')
        config = api.post('/api/v1/delivery/configurations?validation_run_id='+validation['id'], {'name':'Volume chained '+uuid4().hex[:8], **draft})
    automation = api.post('/api/v1/delivery/automations', {'name':'Volume Intake→Delivery','configuration_id':config['id'],
        'settings':{'mode':'CHAINED','source_policy':'INTAKE_OUTPUT','intake_configuration_id':contract['id'],
                    'starts_at':datetime.now(UTC).isoformat(),'timezone':'America/Bogota','allow_empty':False,'allow_warnings':False}})
    with phase(report,'intake_pyspark',directory,context):
        run = api.post('/api/v1/intake/runs',{'contract_id':contract['id'],'dataset_version_id':version_id,'requested_engine':'PYSPARK'},expected=(202,))
        OWNED_OPERATIONS.append('/runs/'+run['id'])
        accepted = wait(api,'/runs/'+run['id'])
        if accepted['status']!='SUCCESS' or accepted['decision']!='APPROVED' or accepted['metrics']['total_rows']!=fixture['rows']:
            raise RuntimeError('Intake no aprobó exactamente la población completa.')
        observed = materialized_hash(directory,context,accepted['output_version_id'])
        if observed['canonical_rows_sha256']!=fixture['canonical_rows_sha256'] or observed['rows']!=fixture['rows']:
            raise RuntimeError('El accepted output cambió algún valor observado.')
        report['intake']={'run_id':run['id'],'output_version_id':accepted['output_version_id'],'metrics':accepted['metrics'],'accepted':observed}
    with phase(report,'chained_delivery',directory,context):
        occurrence = wait(api,'/delivery/automations/'+automation['id']+'/occurrences',predicate=lambda body:bool(body['items'] and body['items'][0].get('run_id')))
        occurrence = occurrence['items'][0]
        OWNED_OPERATIONS.append('/runs/'+occurrence['run_id'])
        delivered = wait(api,'/runs/'+occurrence['run_id'])
        if delivered['status']!='SUCCESS' or delivered['decision']!='COMMITTED':
            raise RuntimeError('Delivery encadenado no confirmó su transacción SQL.')
        receipt = wait(api,'/runs/'+delivered['id'],predicate=lambda body:bool(body.get('metrics',{}).get('receipt_artifact_id')))
        table = draft['target']['table_name']
        target = target_hash(directory,context,schema,table)
        if target['rows']!=fixture['rows'] or target['canonical_rows_sha256']!=fixture['canonical_rows_sha256']:
            raise RuntimeError('El destino SQL no conserva toda la población y valores accepted.')
        report['delivery']={'run_id':delivered['id'],'occurrence_id':occurrence['id'],'source_version_id':occurrence['dataset_version_id'],
            'target_rows':target['rows'],'target':target,'receipt_artifact_id':receipt['metrics']['receipt_artifact_id']}
        if occurrence['dataset_version_id']!=accepted['output_version_id']:
            raise RuntimeError('El encadenado eligió una versión diferente a la salida del Intake disparador.')
    with phase(report,'personal_inbox',directory,context):
        wanted = {source['id'],run['id'],delivered['id']}
        inbox = wait(api,'/notifications/inbox?limit=100',timeout=60,predicate=lambda body:wanted<= {item['resource_id'] for item in body['items']})
        report['inbox']={'resource_ids':sorted(wanted),'notifications':[item['id'] for item in inbox['items'] if item['resource_id'] in wanted]}
    return report


def sql_acquisition(api,directory,context,fixture,connection,schema,reader,report):
    psql(directory,context,f'CREATE TABLE {schema}.source(record_id text,amount numeric(24,8),region text,observed text,payload text); GRANT SELECT ON {schema}.source TO {reader};')
    with phase(report,'postgres_fixture_copy',directory,context):
        psql(directory,context,f'COPY {schema}.source FROM STDIN WITH (FORMAT CSV,HEADER true);',input_path=Path(fixture['formats']['CSV']['path']))
    with phase(report,'postgres_acquisition',directory,context):
        created = api.keyed('/connections/'+connection['id']+'/acquisitions',{'name':'Volume SQL '+uuid4().hex[:8],'schema_name':schema,'object_name':'source'},'pg-'+uuid4().hex)
        finished = wait(api,'/acquisitions/'+created['acquisition']['id'])
        if finished['status']!='SUCCESS': raise RuntimeError('La adquisición PostgreSQL real falló.')
        observed = materialized_hash(directory,context,finished['output_version_id'])
        if observed['canonical_rows_sha256']!=fixture['canonical_rows_sha256']: raise RuntimeError('SQL cambió valores observados.')
        refreshed = api.keyed('/datasets/'+created['dataset']['id']+'/acquisitions/refresh',{},'refresh-'+uuid4().hex)
        new = wait(api,'/acquisitions/'+refreshed['id'])
        if new['status']!='SUCCESS' or new['output_version_id']==finished['output_version_id']: raise RuntimeError('Refresh no publicó una versión nueva.')
        report['postgres']={'acquisition_id':finished['id'],'refresh_id':new['id'],'rows':observed['rows'],'canonical_rows_sha256':observed['canonical_rows_sha256']}
    with phase(report,'postgres_cancel',directory,context):
        cancelled=api.keyed('/datasets/'+created['dataset']['id']+'/acquisitions/refresh',{},'cancel-'+uuid4().hex)
        OWNED_OPERATIONS.append('/acquisitions/'+cancelled['id'])
        active=wait(api,'/acquisitions/'+cancelled['id'],timeout=60,
            predicate=lambda value:value['status']=='RUNNING' and value['processed_rows']>0)
        api.post('/api/v1/acquisitions/'+cancelled['id']+'/cancel',{},expected=(200,))
        stopped=wait(api,'/acquisitions/'+cancelled['id'],timeout=60)
        if stopped['status']!='CANCELLED' or stopped['output_version_id'] is not None:
            raise RuntimeError('La cancelación de un cursor real publicó una versión parcial.')
        versions=api.get('/api/v1/datasets/'+created['dataset']['id'])['versions']
        if len(versions)!=2: raise RuntimeError('La cancelación SQL alteró el historial de versiones completas.')
        report['postgres']['cancel']={'acquisition_id':cancelled['id'],'rows_before_cancel':active['processed_rows'],
            'terminal':stopped['status'],'output_version_id':stopped['output_version_id'],'versions_after_cancel':len(versions)}


def interrupted_acquisition(api,directory,context,fixture,report):
    """A real process loss, expired lease, retry and a single complete publication."""
    path=Path(fixture['formats']['CSV']['path'])
    with phase(report,'worker_restart_lease_recovery',directory,context):
        upload=api.binary(path)
        dataset=api.post('/api/v1/datasets',{'name':'Volume interrupted '+uuid4().hex[:8]})
        run=api.keyed('/datasets/'+dataset['id']+'/acquisitions',{'upload_id':upload['upload']['id']},'recovery-'+uuid4().hex)
        OWNED_OPERATIONS.append('/acquisitions/'+run['id'])
        active=wait(api,'/acquisitions/'+run['id'],timeout=60,
            predicate=lambda value:value['status']=='RUNNING' and value['processed_rows']>=10000)
        certification.assert_main_unchanged(context)
        certification.compose(directory,context,['kill','--signal','SIGKILL','acquisition-worker'])
        try:
            certification.compose(directory,context,['up','--no-build','--detach','--no-deps','acquisition-worker'])
            complete=wait(api,'/acquisitions/'+run['id'])
        finally:
            # Restore only the worker this scenario interrupted, including failure.
            certification.compose(directory,context,['up','--no-build','--detach','--no-deps','acquisition-worker'])
        versions=api.get('/api/v1/datasets/'+dataset['id'])['versions']
        if (complete['status']!='SUCCESS' or complete['attempts']<2 or len(versions)!=1
                or complete['attempt_id']==active['attempt_id']):
            raise RuntimeError('La recuperación no cercó el intento anterior y publicó exactamente una versión.')
        verified=materialized_hash(directory,context,complete['output_version_id'])
        if verified['canonical_rows_sha256']!=fixture['canonical_rows_sha256']:
            raise RuntimeError('La recuperación alteró valores o publicó una salida incompleta.')
        report['recovery']={'acquisition_id':run['id'],'attempts':complete['attempts'],
            'old_attempt_id':active['attempt_id'],'final_attempt_id':complete['attempt_id'],
            'versions':len(versions),'lease_seconds':90,**verified}
        certification.assert_main_unchanged(context)


def staged_corruption(api,directory,context,fixture,report):
    """Only this new private RECEIVED staging object is intentionally changed."""
    with phase(report,'source_hash_corruption',directory,context):
        # The preceding operations are terminal; no unrelated acquisition may run.
        pending=api.get('/api/v1/acquisitions?limit=100')['items']
        if any(item['status'] in {'QUEUED','RUNNING'} for item in pending):
            raise RuntimeError('No se puede interrumpir el worker con una adquisición ajena activa.')
        certification.compose(directory,context,['stop','--timeout','10','acquisition-worker'])
        try:
            upload=api.binary(Path(fixture['formats']['CSV']['path']))
            dataset=api.post('/api/v1/datasets',{'name':'Volume source integrity '+uuid4().hex[:8]})
            run=api.keyed('/datasets/'+dataset['id']+'/acquisitions',{'upload_id':upload['upload']['id']},'integrity-'+uuid4().hex)
            OWNED_OPERATIONS.append('/acquisitions/'+run['id'])
            script="""from pathlib import Path
from trackvance.db import SessionLocal
from trackvance.acquisition_models import AcquisitionUpload
from trackvance.artifactstore import artifact_store
with SessionLocal() as db:
 upload=db.get(AcquisitionUpload,__UPLOAD__)
 assert upload.status=='REGISTERED'
 path=artifact_store.checked_path(upload.path)
 assert path.is_relative_to(artifact_store.location('tmp'))
 with path.open('ab') as stream: stream.write(b'corrupt-private-fixture')
""".replace('__UPLOAD__',repr(upload['upload']['id']))
            certification.compose(directory,context,['exec','-T','api','python','-c',script])
        finally:
            certification.compose(directory,context,['up','--no-build','--detach','--no-deps','acquisition-worker'])
        complete=wait(api,'/acquisitions/'+run['id'])
        if complete['status']!='FAILED' or complete['error_code']!='ARTIFACT_INTEGRITY_ERROR' or complete['output_version_id'] is not None:
            raise RuntimeError('La corrupción privada de staging no impidió publicar la versión.')
        if api.get('/api/v1/datasets/'+dataset['id'])['versions']:
            raise RuntimeError('La corrupción creó una DatasetVersion parcial.')
        report['corruption']={'acquisition_id':run['id'],'status':complete['status'],
            'error_code':complete['error_code'],'output_version_id':complete['output_version_id'],'versions':0}
        report['corruption']['staging_cleanup'] = cleanup_corrupt_staging(directory, context, run['id'])
        certification.assert_main_unchanged(context)


def main():
    global RESOURCE_MEMORY_BUDGET
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context',type=Path,required=True)
    parser.add_argument('--sizes',type=int,nargs='+',default=[100,500,1024])
    parser.add_argument('--rows',type=int,default=1_000_000)
    parser.add_argument('--mode',choices=['varied','compressible'],default='varied')
    parser.add_argument('--prepare-only',action='store_true')
    parser.add_argument('--optional-large',action='store_true')
    parser.add_argument('--no-sql',action='store_true')
    parser.add_argument('--verify-browser-report',type=Path)
    args=parser.parse_args()
    directory,context=certification.load_context(args.context)
    if args.verify_browser_report:
        print(json.dumps(verify_browser_report(directory, context, args.verify_browser_report)))
        return
    fixtures=directory/'volume-fixtures'; report={'schema_version':1,'project':context['project'],'rows':args.rows,'mode':args.mode,'tiers':[]}
    if args.prepare_only:
        for size in args.sizes:
            report['tiers'].append({'status':'PREPARED_ONLY','fixture':generate(fixtures,size,args.rows,args.mode)})
        (directory/'volume-fixtures.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8'); return
    certification.assert_main_unchanged(context)
    api=VolumeApi(f'http://127.0.0.1:{context["port"]}',1800)
    session=api.post('/api/v1/auth/demo',{},expected=(200,)); api.csrf=session['csrf_token']
    limits=api.get('/api/v1/system/engines')['limits']['acquisition']
    docker=json.loads(certification.command(['docker','info','--format','{{json .}}']))
    RESOURCE_MEMORY_BUDGET=min(6*GIB,max(GIB,docker['MemTotal']-2*GIB))
    report['resource_profile']={'docker_memory_bytes':docker['MemTotal'],'backend_cgroup_budget_bytes':RESOURCE_MEMORY_BUDGET,
        'host_disk_reserve_bytes':5*GIB,'effective_acquisition_limits':limits,'mandatory_tiers':[100,500,1024]}
    destination,connection,schema,reader=destination_fixture(api,directory,context,uuid4().hex[:8])
    report['playwright']={'destination_id':destination['id'],'destination_version_id':destination['destination_version_id'],'schema':schema}
    sizes=args.sizes+([2048,5120] if args.optional_large else [])
    try:
        for size in sizes:
            tier={'target_mib':size,'status':'RUNNING','phases':{}}; report['tiers'].append(tier)
            required_disk=size*MIB*12+10*GIB
            if shutil.disk_usage(ROOT).free<required_disk or docker['MemTotal']<6*GIB or size*MIB+64*MIB>limits['max_upload_bytes'] or size*MIB*2>limits['max_observed_bytes']:
                tier.update(status='NOT_RUN_RESOURCE_LIMIT' if size in {2048,5120} else 'FAIL_RESOURCE_LIMIT',reason='Tier exceeds configured upload/observed bound, measured Docker budget or disk reserve.',
                    required_disk_bytes=required_disk,disk_free_bytes=shutil.disk_usage(ROOT).free,docker_memory_bytes=docker['MemTotal'],effective_limits=limits)
                if size not in {2048,5120}: raise RuntimeError('El entorno no permite ejecutar un tier obligatorio; configura explícitamente el perfil aislado.')
                continue
            tier['fixture']=fixture=generate(fixtures,size,args.rows,args.mode)
            tier['formats']={}
            for kind,metadata in fixture['formats'].items():
                with phase(tier,'receive_acquire_profile_'+kind,directory,context):
                    dataset,run=acquire(api,Path(metadata['path']),f'Volume {size}MiB {kind} '+uuid4().hex[:8])
                    verified=materialized_hash(directory,context,run['output_version_id'])
                    if verified['rows']!=args.rows or verified['canonical_rows_sha256']!=fixture['canonical_rows_sha256']:
                        raise RuntimeError('El formato no conservó toda la población y sus valores.')
                    profile=api.get('/api/v1/dataset-versions/'+run['output_version_id']+'/profile')
                    profile_summary = validate_profile(profile, fixture)
                    tier['formats'][kind]={'dataset_id':dataset['id'],'acquisition_id':run['id'],'version_id':run['output_version_id'],
                                           'profile':profile_summary,**verified}
                if kind=='CSV':
                    chain(api,directory,context,fixture,dataset,run,destination,schema,tier)
            if size==args.sizes[0]:
                if not args.no_sql: sql_acquisition(api,directory,context,fixture,connection,schema,reader,tier)
                interrupted_acquisition(api,directory,context,fixture,tier)
                staged_corruption(api,directory,context,fixture,tier)
            tier['status']='PASS'
            (directory/'volume-evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    except Exception as error:
        if report['tiers'] and report['tiers'][-1]['status']=='RUNNING': report['tiers'][-1].update(status='FAIL',error_type=type(error).__name__,error=str(error)[:500])
        # Only operations registered by this invocation are cooperatively stopped.
        # A failed resource guard never leaves an unobserved heavy job running.
        for path in OWNED_OPERATIONS:
            try:
                current=api.get('/api/v1'+path)
                if current.get('status') in {'QUEUED','RUNNING'}:
                    api.post('/api/v1'+path+'/cancel',{},expected=(200,202))
            except (RuntimeError, OSError, ValueError) as cleanup_error:
                # Preserve the initial error; report an unavailable owned-job
                # cleanup without exporting HTTP bodies or credentials.
                report.setdefault('cleanup_errors', []).append({'path':path,'status':'UNAVAILABLE',
                                                                'error_type':type(cleanup_error).__name__})
        raise
    finally:
        (directory/'volume-evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        certification.assert_main_unchanged(context)
    print(json.dumps({'report':str(directory/'volume-evidence.json'),'tiers':[{key:tier.get(key) for key in ('target_mib','status','reason')} for tier in report['tiers']]}))


if __name__=='__main__': main()
