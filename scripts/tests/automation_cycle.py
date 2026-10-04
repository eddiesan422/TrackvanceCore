"""Certify scheduling, chaining, SQL effects and personal inbox on an isolated stack.

Execute inside its backend container with TRACKVANCE_CERTIFICATION_PROJECT set.
This runner never starts, stops or deletes Docker resources. The caller owns the
disposable project and copies the sanitized JSON evidence out afterwards.
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import timedelta
from pathlib import Path


def require_isolated_environment():
    project = os.environ.get("TRACKVANCE_CERTIFICATION_PROJECT", "")
    if not re.fullmatch(r"trackvance-v070-test-[a-z0-9-]+-[a-f0-9]{12}", project):
        raise RuntimeError("Se requiere un proyecto exclusivo trackvance-v070-test-{suite}-{uuid12}.")
    from sqlalchemy.engine import make_url

    url = make_url(os.environ.get("DATABASE_URL", ""))
    if url.get_backend_name() != "postgresql" or url.database != "tv_v070_test" or url.username != "tv_v070_test":
        raise RuntimeError("La certificación requiere la metadata PostgreSQL aislada tv_v070_test.")
    return project, url


class Client:
    def __init__(self, base):
        self.base, self.csrf = base.rstrip("/"), ""
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, path, body=None, *, expected=200):
        headers = {"X-CSRF-Token": self.csrf}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        request = urllib.request.Request(self.base + "/api/v1" + path, data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=20) as response:
                status, value = response.status, json.load(response)
        except urllib.error.HTTPError as error:
            status, value = error.code, json.load(error)
        if status != expected:
            raise AssertionError(f"HTTP {status} inesperado para {path.split('?')[0]}; se esperaba {expected}.")
        if "csrf_token" in value:
            self.csrf = value["csrf_token"]
        return value


def wait_for(callback, predicate, *, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = callback()
        if predicate(value):
            return value
        time.sleep(1)
    raise AssertionError("La condición de certificación no se cumplió antes del límite.")


def certify_chain_decisions(client, engine, sessions, *, source_id, dataset_id,
                            draft, organization_id, actor_id, nonce):
    """Real API -> Intake -> outbox -> SQL; no fabricated terminal run/outbox."""
    require_isolated_environment()
    if not re.fullmatch(r'[a-f0-9]{12}', nonce):
        raise RuntimeError('La tabla de negativas exige una identidad sintética propia.')
    from decimal import Decimal

    from sqlalchemy import func, select, text

    from trackvance import events
    from trackvance.artifactstore import storage_provider
    from trackvance.automation import consume_intake_event
    from trackvance.automation_models import DeliveryOccurrence, OutboxEvent
    from trackvance.dataset_scans import iter_version_batches
    from trackvance.db import iso, utcnow
    from trackvance.models import Dataset, DatasetVersion, Run, User
    from trackvance.services import create_version

    table = f"chain_decisions_{nonce}"
    with engine.begin() as connection:
        connection.execute(text(f'CREATE TABLE automation_cert."{table}" (id text NOT NULL, amount numeric(18,2) NOT NULL)'))
    effective = {**draft, 'target': {**draft['target'], 'table_name': table}}
    validation = client.call('/delivery/validations', effective, expected=202)
    validation = wait_for(lambda: client.call(f"/delivery/validations/{validation['id']}"),
                         lambda value: value['status'] in {'SUCCESS', 'FAILED', 'FAILED_PRECONDITION'})
    assert validation['status'] == 'SUCCESS' and validation['result']['status'] == 'PASS'
    publication = client.call(f"/delivery/configurations?validation_run_id={validation['id']}",
        {'name': f'Chain decisions {nonce}', 'owner': 'Certification', **effective}, expected=201)
    cases = [('warnings_default', 'WARNING', False, 'APPROVED_WITH_WARNINGS', 'INTAKE_DECISION_NOT_ACCEPTED'),
             ('warnings_opt_in', 'WARNING', True, 'APPROVED_WITH_WARNINGS', None),
             ('rejected', 'ERROR', False, 'REJECTED', 'INTAKE_DECISION_NOT_ACCEPTED'),
             ('empty', None, False, 'APPROVED', 'EMPTY_INPUT_BLOCKED')]
    results = []
    for case, severity, allow_warnings, decision, reason in cases:
        input_id = source_id
        if case == 'empty':
            path = storage_provider.temporary_path('.csv')
            try:
                path.write_text('id,amount\n', encoding='utf-8')
                with sessions() as db:
                    version = create_version(db, db.get(Dataset, dataset_id), path, 'empty-intake.csv',
                                             actor=db.get(User, actor_id).name)
                    db.commit()
                    input_id = version.id
            finally:
                path.unlink(missing_ok=True)
        config = {'required_columns': ['id'], 'max_error_rate': 0}
        if severity:
            config['rules'] = [{'type': 'length', 'column': 'id', 'severity': severity,
                                'parameters': {'min': 3, 'max': 20}}]
        contract = client.call('/intake/contracts', {'name': f'{case} {nonce}',
                              'dataset_id': dataset_id, 'config': config}, expected=201)
        automation = client.call('/delivery/automations', {'configuration_id': publication['id'],
            'name': f'{case} chain {nonce}', 'settings': {'mode': 'CHAINED', 'timezone': 'America/Bogota',
                'starts_at': iso(utcnow() - timedelta(seconds=1)), 'source_policy': 'INTAKE_OUTPUT',
                'intake_configuration_id': contract['id'], 'allow_warnings': allow_warnings}}, expected=201)
        with engine.connect() as connection:
            before = connection.scalar(text(f'SELECT count(*) FROM automation_cert."{table}"'))
        run = client.call('/intake/runs', {'contract_id': contract['id'], 'dataset_version_id': input_id}, expected=202)
        run = wait_for(lambda identifier=run['id']: client.call(f"/runs/{identifier}"), lambda value: value['status'] in {'SUCCESS', 'FAILED', 'FAILED_PRECONDITION'})
        assert run['status'] == 'SUCCESS' and run['decision'] == decision and run['output_version_id']
        def observed_occurrence(automation_id=automation['id'], source_run_id=run['id']):
            return next((item for item in client.call(f"/delivery/automations/{automation_id}/occurrences")['items']
                         if item['source_run_id'] == source_run_id), {})
        occurrence = wait_for(observed_occurrence, lambda value: value.get('status') in {'SUCCESS', 'SKIPPED', 'FAILED', 'UNKNOWN'})
        assert occurrence['reason_code'] == reason
        records_hash = None
        with engine.connect() as connection:
            after = connection.scalar(text(f'SELECT count(*) FROM automation_cert."{table}"'))
        if reason:
            assert occurrence['status'] == 'SKIPPED' and occurrence['run_id'] is None and after == before
        else:
            assert occurrence['status'] == 'SUCCESS' and occurrence['dataset_version_id'] == run['output_version_id'] != input_id
            delivery = client.call(f"/runs/{occurrence['run_id']}")
            assert delivery['decision'] == 'COMMITTED' and after == before + 2
            with sessions() as db:
                output = db.get(DatasetVersion, run['output_version_id'])
                assert output.row_count == 2
                accepted = [tuple(row) for batch in iter_version_batches(db, output) for row in batch.iter_rows()]
            with engine.connect() as connection:
                sql = connection.execute(text(f'SELECT id,amount FROM automation_cert."{table}" ORDER BY id')).fetchmany(3)
            assert len(sql) == 2 and all(isinstance(row[1], Decimal) for row in sql)
            sql_rows = [(row[0], format(row[1], 'f')) for row in sql]
            assert sorted(accepted) == sql_rows == [('A', '12.25'), ('B', '20.50')]
            records_hash = hashlib.sha256(json.dumps(sql_rows, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
        with sessions() as db:
            event = db.scalar(select(OutboxEvent).where(OutboxEvent.organization_id == organization_id,
                OutboxEvent.aggregate_id == run['id'], OutboxEvent.event_type == 'RUN_TERMINAL'))
            assert event is not None
            delivery_count = db.scalar(select(func.count()).select_from(Run).where(Run.organization_id == organization_id, Run.module == 'DELIVERY'))
            repeated = events.append_event(db, organization_id=event.organization_id, dedupe_key=event.dedupe_key,
                event_type=event.event_type, aggregate_type=event.aggregate_type, aggregate_id=event.aggregate_id,
                module=event.module, payload=event.payload)
            assert repeated.id == event.id
            consume_intake_event(db, event)
            consume_intake_event(db, event)
            db.commit()
            assert db.scalar(select(func.count()).select_from(DeliveryOccurrence).where(DeliveryOccurrence.automation_id == automation['id'])) == 1
            assert db.scalar(select(func.count()).select_from(Run).where(Run.organization_id == organization_id, Run.module == 'DELIVERY')) == delivery_count
        with engine.connect() as connection:
            assert connection.scalar(text(f'SELECT count(*) FROM automation_cert."{table}"')) == after
        results.append({'case': case, 'status': 'PASS', 'intake_decision': decision,
            'occurrence_status': occurrence['status'], 'reason_code': reason, 'sql_rows_added': after - before,
            'exact_accepted_sql_sha256': records_hash, 'duplicate_event_no_replay': 'PASS',
            'fixture': 'REAL_INTAKE_EMPTY_HEADER_ONLY' if case == 'empty' else 'REAL_INTAKE_RULES'})
    return results


def main():
    project, url = require_isolated_environment()
    from sqlalchemy import func, select, text

    from trackvance.artifactstore import storage_provider
    from trackvance.automation import AutomationError, claim_delivery_target
    from trackvance.automation_models import (
        DeliveryOccurrence,
        EventConsumption,
        InternalNotification,
    )
    from trackvance.db import SessionLocal, engine, iso, utcnow
    from trackvance.models import Configuration, Dataset, Run, User
    from trackvance.services import create_version

    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://api:8000")
    parser.add_argument("--evidence", type=Path, default=Path("/var/lib/trackvance/certification/automation"))
    args = parser.parse_args()
    client = Client(args.api_url)
    session = client.call("/auth/demo", {})
    actor_id, organization_id = session["user"]["id"], session["organization"]["id"]
    nonce = uuid.uuid4().hex[:12]
    table = f"publication_{nonce}"
    with engine.begin() as connection:
        connection.execute(text('CREATE SCHEMA IF NOT EXISTS automation_cert'))
        connection.execute(text(f'CREATE TABLE automation_cert."{table}" (id text NOT NULL, amount numeric(18,2) NOT NULL)'))
    path = storage_provider.temporary_path('.csv')
    path.write_text('id,amount\nA,12.25\nB,20.50\n', encoding='utf-8')
    with SessionLocal() as db:
        actor = db.get(User, actor_id)
        dataset = Dataset(organization_id=organization_id, name=f"Automation certification {nonce}")
        db.add(dataset)
        db.flush()
        version = create_version(db, dataset, path, 'certification.csv', actor=actor.name)
        db.commit()
        source_id, dataset_id = version.id, dataset.id
    destination = client.call('/delivery/destinations', {
        'name': f'Automation target {nonce}', 'sink_type': 'POSTGRESQL', 'host': url.host,
        'port': url.port or 5432, 'database': url.database, 'username': url.username,
        'password': url.password, 'options': {'sslmode': 'disable', 'connect_timeout': 5, 'query_timeout': 60}}, expected=201)
    draft = {'schema_version': 1, 'dataset_version_id': source_id, 'destination_id': destination['id'],
        'destination_version_id': destination['destination_version_id'],
        'target': {'mode': 'EXISTING_TABLE', 'schema_name': 'automation_cert', 'table_name': table, 'create_schema': False},
        'columns': [{'source_name': 'id', 'target_name': 'id', 'target_type': 'STRING', 'ordinal': 0, 'nullable': False, 'length': 40},
                    {'source_name': 'amount', 'target_name': 'amount', 'target_type': 'DECIMAL', 'ordinal': 1, 'nullable': False, 'precision': 18, 'scale': 2}],
        'write_strategy': 'APPEND', 'upsert_keys': [], 'audit_columns_enabled': False}
    validation = client.call('/delivery/validations', draft, expected=202)
    validation = wait_for(lambda: client.call(f"/delivery/validations/{validation['id']}"),
                          lambda value: value['status'] in {'SUCCESS', 'FAILED', 'FAILED_PRECONDITION'})
    assert validation['status'] == 'SUCCESS' and validation['result']['status'] == 'PASS'
    configuration = client.call(f"/delivery/configurations?validation_run_id={validation['id']}", {'name': f'Automation publication {nonce}', 'owner': 'Certification', 'description': 'Disposable real target', **draft}, expected=201)
    scheduled = client.call('/delivery/automations', {'configuration_id': configuration['id'], 'name': f'Scheduled certification {nonce}',
        'settings': {'mode': 'ONCE', 'timezone': 'America/Bogota', 'starts_at': iso(utcnow() + timedelta(seconds=5)), 'source_policy': 'FIXED_VERSION'}}, expected=201)
    occurrences_path = f"/delivery/automations/{scheduled['id']}/occurrences"
    occurrences = wait_for(lambda: client.call(occurrences_path), lambda value: bool(value['items']) and value['items'][0]['status'] == 'SUCCESS')
    first = occurrences['items'][0]
    assert first['dataset_version_id'] == source_id and first['decision'] == 'COMMITTED'
    with engine.connect() as connection:
        assert connection.scalar(text(f'SELECT count(*) FROM automation_cert."{table}"')) == 2
    manual_path = f"/delivery/automations/{scheduled['id']}/dispatch"
    skipped = client.call(manual_path, {'request_key': 'stable-no-repeat'}, expected=202)
    assert skipped['reason_code'] == 'VERSION_ALREADY_PROCESSED'
    assert client.call(manual_path, {'request_key': 'stable-no-repeat'}, expected=202)['id'] == skipped['id']
    client.call(manual_path, {'request_key': 'stable-no-repeat', 'repeat': True}, expected=409)
    repeated = client.call(manual_path, {'request_key': 'explicit-repeat', 'repeat': True}, expected=202)
    wait_for(lambda: client.call(f"/runs/{repeated['run_id']}"), lambda value: value['status'] == 'SUCCESS')
    with engine.connect() as connection:
        assert connection.scalar(text(f'SELECT count(*) FROM automation_cert."{table}"')) == 4
    with SessionLocal() as db:
        contract = Configuration(organization_id=organization_id, name=f'Intake chain {nonce}', module='intake', dataset_id=dataset_id,
                                 config={'required_columns': ['id'], 'positive_columns': ['amount'], 'max_error_rate': 0})
        db.add(contract)
        db.commit()
        contract_id = contract.id
    chained = client.call('/delivery/automations', {'configuration_id': configuration['id'], 'name': f'Chained certification {nonce}',
        'settings': {'mode': 'CHAINED', 'timezone': 'America/Bogota', 'starts_at': iso(utcnow() - timedelta(seconds=1)),
                     'source_policy': 'INTAKE_OUTPUT', 'intake_configuration_id': contract_id}}, expected=201)
    intake = client.call('/intake/runs', {'contract_id': contract_id, 'dataset_version_id': source_id}, expected=202)
    intake = wait_for(lambda: client.call(f"/runs/{intake['id']}"), lambda value: value['status'] in {'SUCCESS', 'FAILED'})
    assert intake['status'] == 'SUCCESS' and intake['decision'] == 'APPROVED'
    chained_occurrences = wait_for(lambda: client.call(f"/delivery/automations/{chained['id']}/occurrences"),
        lambda value: bool(value['items']) and value['items'][0]['status'] == 'SUCCESS')
    chain = chained_occurrences['items'][0]
    assert chain['source_run_id'] == intake['id'] and chain['dataset_version_id'] == intake['output_version_id']
    assert chain['dataset_version_id'] != source_id
    with engine.connect() as connection:
        assert connection.scalar(text(f'SELECT count(*) FROM automation_cert."{table}"')) == 6
    inbox = wait_for(lambda: client.call('/notifications/inbox'), lambda value: any(item['resource_id'] == chain['run_id'] for item in value['items']))
    own_notification = next(item for item in inbox['items'] if item['resource_id'] == chain['run_id'])
    assert own_notification['origin'] == 'CHAINED' and own_notification['decision'] == 'COMMITTED'
    client.call(f"/notifications/inbox/{own_notification['id']}/read", {})
    # Even an administrator from the same organization receives only their own
    # inbox. A second identity is generated solely for this disposable project.
    from argon2 import PasswordHasher

    from trackvance import identity_bootstrap  # noqa: F401

    password = secrets.token_urlsafe(30)
    with SessionLocal() as db:
        other = User(organization_id=organization_id, username=f'other-{nonce}', name='Inbox privacy certification',
            email=f'other-{nonce}@example.test', role='Administrator', password_hash=PasswordHasher().hash(password),
            must_change_password=False)
        db.add(other)
        db.commit()
    second = Client(args.api_url)
    second.call('/auth/login', {'username': f'other-{nonce}', 'password': password})
    assert second.call('/notifications/inbox')['total'] == 0
    assert second.call('/notifications/unread-count')['unread_count'] == 0
    second.call(f"/notifications/inbox/{own_notification['id']}/read", {}, expected=404)
    del password
    with SessionLocal() as db:
        consumer = db.scalar(select(EventConsumption).where(EventConsumption.consumer == 'CHAINING').order_by(EventConsumption.created_at.desc()))
        consumer.status, consumer.lease_owner, consumer.lease_until = 'RUNNING', 'lost-certification-process', utcnow() - timedelta(seconds=1)
        db.commit()
        consumer_id = consumer.id
    def recovered():
        with SessionLocal() as db:
            return db.get(EventConsumption, consumer_id).status
    wait_for(recovered, lambda value: value == 'DONE')
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(DeliveryOccurrence).where(DeliveryOccurrence.automation_id == chained['id'])) == 1
    # Simultaneous metadata claims compete on the physical target's PostgreSQL
    # advisory lock; exactly one caller commits ownership, independent of origin.
    with SessionLocal() as db:
        competitors = [Run(name=f'Concurrency probe {index}', module='DELIVERY', organization_id=organization_id,
            config_id=configuration['id'], dataset_version_id=source_id, initiated_by='Certification', status='QUEUED') for index in range(2)]
        db.add_all(competitors)
        db.commit()
        competitor_ids = [run.id for run in competitors]
    barrier, outcomes = threading.Barrier(2), []
    def compete(identifier):
        with SessionLocal() as db:
            run = db.get(Run, identifier)
            barrier.wait(timeout=20)
            try:
                claim_delivery_target(db, run)
                db.commit()
                outcomes.append('CLAIMED')
            except AutomationError as error:
                db.rollback()
                outcomes.append(error.code)
            except Exception:  # noqa: BLE001 -- report sanitized thread failure to the certification coordinator
                db.rollback()
                outcomes.append('UNEXPECTED_FAILURE')
    threads = [threading.Thread(target=compete, args=(identifier,)) for identifier in competitor_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(outcomes) == ['CLAIMED', 'TARGET_BUSY']
    with SessionLocal() as db:
        for identifier in competitor_ids:
            db.get(Run, identifier).status = 'CANCELLED'
        db.commit()
        notification_count = db.scalar(select(func.count()).select_from(InternalNotification))
    decisions = certify_chain_decisions(client, engine, SessionLocal, source_id=source_id, dataset_id=dataset_id,
        draft=draft, organization_id=organization_id, actor_id=actor_id, nonce=nonce)
    with SessionLocal() as db:
        notification_count = db.scalar(select(func.count()).select_from(InternalNotification))
    report = {'status': 'PASS', 'project': project, 'scheduled_run_id': first['run_id'], 'chained_run_id': chain['run_id'],
        'intake_run_id': intake['id'], 'exact_output_version_id': chain['dataset_version_id'], 'sql_rows': 6,
        'notifications': notification_count, 'cursor_occurrence_unique': 'PASS', 'no_repeat': 'PASS',
        'request_idempotency': 'PASS', 'deliberate_repeat': 'PASS', 'lease_recovery': 'PASS', 'target_concurrency': 'PASS',
        'administrator_personal_inbox': 'PASS', 'cross_user_read_denied': 'PASS',
        'automation_id': scheduled['id'], 'configuration_id': configuration['id'], 'chain_decisions': decisions}
    args.evidence.mkdir(parents=True, exist_ok=True)
    (args.evidence / 'automation-results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
