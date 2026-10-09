"""R085-06 populated 0.8.0 -> 0.8.5 migration and exact backup projection."""
import os
import subprocess
import sys
from pathlib import Path


def test_people_migration_preserves_explicit_links_history_and_backup_projection(tmp_path):
    environment = {**os.environ, "DATABASE_URL": f"sqlite:///{(tmp_path / 'people.db').as_posix()}",
        "TRACKVANCE_STORAGE_DIR": str(tmp_path / "storage"), "TRACKVANCE_STORAGE_ROOT": str(tmp_path / "storage"),
        "DEMO_SEED_ENABLED": "false"}
    script = '''
from datetime import UTC, datetime
from pathlib import Path
from copy import deepcopy
import sys
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, select
from sqlalchemy.orm import Session
from trackvance.db import engine
from trackvance.models import User
from trackvance import identity_bootstrap
sys.path.insert(0, str(Path.cwd().parent / "scripts"))
import verify_storage
config=Config("alembic.ini")
command.upgrade(config, "0017_catalog_reports")
with Session(engine) as db:
    db.add(User(id="assigned-user", name="Historical contact", email="history@example.test", password_hash="fixture", active=False))
    db.commit()
before=MetaData()
before.reflect(bind=engine)
now=datetime.now(UTC)
with engine.begin() as connection:
    for identity, user_id in (("assigned-dataset", "assigned-user"),("owner-text-only", None)):
        connection.execute(before.tables["datasets"].insert().values(id=identity,organization_id="org-trackvance-demo",created_at=now,
            name=identity,description="",domain="Operations",owner="Historical contact",criticality="HIGH",status="ACTIVE",
            governance_version=1,business_owner_id=user_id,steward_id=user_id,technical_custodian_id=None,
            macro_domain_id=None,domain_id=None,information_classification="UNKNOWN",intake_input_dataset_id=None,intake_contract_id=None))
    connection.execute(before.tables["governance_history"].insert().values(id="history",organization_id="org-trackvance-demo",created_at=now,
        dataset_id="assigned-dataset",version=1,actor_id="assigned-user",reason="Historical",snapshot={"schema_version":1,"business_owner_id":"assigned-user"}))
    original={name:[dict(row) for row in connection.execute(select(table)).mappings()] for name,table in before.tables.items() if name!="alembic_version"}
command.upgrade(config,"head")
after=MetaData()
after.reflect(bind=engine)
with engine.connect() as connection:
    rows={name:[dict(row) for row in connection.execute(select(table)).mappings()] for name,table in after.tables.items() if name!="alembic_version"}
fks=[(name,fk.parent.name,fk.column.table.name,fk.column.name) for name,table in after.tables.items() for fk in table.foreign_keys]
previous,previous_fks=verify_storage.project_people_upgrade(rows,fks)
assert previous==original
assert len(rows["governance_people"])==1
person=rows["governance_people"][0]
assert person["user_id"]=="assigned-user" and person["active"] is False
assert rows["governance_history"]==original["governance_history"]
datasets={row["id"]:row for row in rows["datasets"]}
assert datasets["assigned-dataset"]["business_owner_person_id"]==person["id"]
assert datasets["assigned-dataset"]["steward_person_id"]==person["id"]
assert datasets["owner-text-only"]["business_owner_person_id"] is None
for field,value in (("name","Rewritten"),("active",True),("reference","new")):
    damaged=deepcopy(rows)
    damaged["governance_people"][0][field]=value
    try: verify_storage.project_people_upgrade(damaged,fks)
    except ValueError: pass
    else: raise AssertionError("Unprovable migration was accepted")
print("R085-06 populated migration, history and state8 projection PASS")
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
        env=environment, capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "R085-06 populated migration" in result.stdout


def test_populated_080_postgres_harness_preservation_logic_on_disposable_sqlite(tmp_path):
    environment = {**os.environ, "DATABASE_URL": f"sqlite:///{(tmp_path / 'harness.db').as_posix()}",
        "TRACKVANCE_STORAGE_DIR": str(tmp_path / "storage"), "DEMO_SEED_ENABLED": "false"}
    script = r"""
from pathlib import Path
import sys
from alembic import command
from alembic.config import Config
from trackvance import models
from trackvance.db import engine
sys.path.insert(0,str(Path.cwd().parent/'scripts'))
import check_postgres_migrations as checker
config=Config('alembic.ini')
with engine.connect() as connection:
    config.attributes['connection']=connection
    command.upgrade(config,'0015_sentinel_execution_identity')
    connection.commit()
    checker.seed_pre_corrections_baseline(connection)
    connection.commit()
    command.upgrade(config,'0017_catalog_reports')
    connection.commit()
    report=checker.verify_people_preservation(connection,config)
    assert report['source_version']=='0.8.0' and report['target_version']=='0.8.5'
    assert report['exact_historical_projection']=='PASS' and report['roundtrip']=='PASS'
    assert report['tables_before']==55 and report['tables_after']==56
print('Populated source080 harness logic PASS on SQLite; PostgreSQL execution remains separate')
"""
    result=subprocess.run([sys.executable,'-c',script],cwd=Path(__file__).resolve().parents[1],env=environment,
        capture_output=True,text=True,timeout=60,check=False)
    assert result.returncode==0,result.stdout+result.stderr
