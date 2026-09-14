"""Idempotent, small, deterministic fixtures. Demo access is explicit in the UI."""

import os
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from sqlalchemy import select

from .config import BACKEND_DIR, DEMO_ENABLED, DEMO_USER_ID
from .db import Base, SessionLocal, engine, iso, utcnow
from .models import AuditEvent, Configuration, Dataset, DatasetVersion, Finding, Job, Run, User
from .services import audit, create_exception, create_version, enqueue, execute_run

DEMO_DIR = BACKEND_DIR.parent / "demo"


def seed_demo():
    if not DEMO_ENABLED:
        return
    with SessionLocal() as db:
        if db.scalar(select(AuditEvent).where(AuditEvent.event_type == "DEMO_SEED_COMPLETED")):
            return
        now = utcnow()
        user = db.get(User, DEMO_USER_ID)
        if not user:
            password = os.getenv("TRACKVANCE_ADMIN_PASSWORD") or secrets.token_urlsafe(48)
            user = User(id=DEMO_USER_ID, name="Equipo Trackvance", email=os.getenv("TRACKVANCE_ADMIN_EMAIL", "demo@trackvance.local").lower(), role="Administrator", password_hash=PasswordHasher().hash(password))
            db.add(user)
        db.flush()

        def dataset(identifier, name, domain, description, owner):
            row = db.get(Dataset, identifier)
            if not row:
                row = Dataset(id=identifier, name=name, domain=domain, description=description, owner=owner, criticality="HIGH", created_at=now - timedelta(days=8))
                db.add(row)
                db.flush()
            return row

        def version(dataset_row, filename, label=None, when=None):
            name = label or filename
            row = db.scalar(select(DatasetVersion).where(DatasetVersion.dataset_id == dataset_row.id, DatasetVersion.filename == name))
            return row or create_version(db, dataset_row, DEMO_DIR / filename, name, "Demo", "DEMO", when or now)

        def config(identifier, name, module, dataset_row, rules, target=None, description=""):
            row = db.get(Configuration, identifier)
            if not row:
                row = Configuration(id=identifier, name=name, module=module, dataset_id=dataset_row.id, target_dataset_id=target.id if target else None, config=rules, owner=dataset_row.owner, description=description, created_at=now - timedelta(days=7))
                db.add(row)
                db.flush()
            return row

        def run(configuration, source, target=None, when=None):
            row = db.scalar(select(Run).where(Run.config_id == configuration.id, Run.dataset_version_id == source.id, Run.initiated_by == "Demo"))
            if not row:
                row = enqueue(db, configuration, source, target, "Demo")
                row.created_at = when or now
            if row.status != "SUCCESS":
                job = db.scalar(select(Job).where(Job.run_id == row.id))
                if job is None:
                    raise ValueError("La ejecución demo necesita su trabajo persistido.")
                job.status, job.lease_owner, job.lease_until = "RUNNING", "demo-seed", now + timedelta(minutes=10)
                execute_run(db, row, lease_owner="demo-seed", observed_at=when or now)
                job.status, job.lease_until = row.status, None
                db.commit()
            return row

        orders = dataset("demo-dataset-orders", "Pedidos comerciales", "Comercial", "Carga de pedidos con campos obligatorios, claves duplicadas e importes inválidos para validar Intake.", "Laura Méndez")
        source = dataset("demo-dataset-payments", "Pagos · Sistema transaccional", "Finanzas", "Movimientos de origen para el control de conciliación diaria.", "Carlos Ruiz")
        target = dataset("demo-dataset-bank", "Pagos · Extracto bancario", "Finanzas", "Contraparte bancaria con diferencias de valor y registros sin correspondencia.", "Carlos Ruiz")
        monitored = dataset("demo-dataset-daily", "Pedidos · Seguimiento diario", "Operaciones", "Siete versiones de ejemplo: seis cargas estables y una caída de volumen con nulos.", "Ana Torres")

        intake_config = config("demo-contract-orders", "Validación de pedidos", "intake", orders, {"required_columns": ["pedido_id", "cliente", "valor"], "unique_columns": ["pedido_id"], "numeric_columns": ["valor"], "positive_columns": ["valor"], "max_error_rate": .05}, description="Validación de estructura y calidad antes de admitir pedidos.")
        recon_config = config("demo-control-payments", "Conciliación de pagos diarios", "recon", source, {"key_columns": ["pedido_id"], "amount_column": "valor", "tolerance": "0.01"}, target, "Cruce por pedido e importe con tolerancia absoluta de 0.01.")
        monitor_config = config("demo-monitor-daily", "Salud del flujo de pedidos", "sentinel", monitored, {"required_columns": ["pedido_id", "cliente", "valor"], "null_columns": ["pedido_id", "cliente"], "max_null_rate": .05, "max_volume_change_pct": 15, "max_age_hours": 48}, description="Supervisa nulos, estructura, actualización y variación frente a la carga anterior.")

        for index in range(7):
            when = now - timedelta(days=6 - index, hours=1)
            filename = "sentinel_anomalia.csv" if index == 6 else "sentinel_normal.csv"
            observed = version(monitored, filename, f"pedidos_dia_{index + 1:02d}.csv", when)
            run(monitor_config, observed, when=when + timedelta(minutes=2))
        run(intake_config, version(orders, "intake_pedidos.csv", when=now - timedelta(minutes=35)), when=now - timedelta(minutes=30))
        run(recon_config, version(source, "recon_origen.csv", when=now - timedelta(minutes=25)), version(target, "recon_destino.csv", when=now - timedelta(minutes=24)), when=now - timedelta(minutes=20))

        findings = db.scalars(select(Finding).order_by(Finding.created_at, Finding.code)).all()
        for index, finding in enumerate(findings[:6]):
            case = create_exception(db, finding, "Demo")
            case.owner = ["Ana Torres", "Carlos Ruiz", "Laura Méndez"][index % 3]
            state = ["OPEN", "INVESTIGATING", "WAITING_EXTERNAL", "OPEN", "RESOLVED", "ACCEPTED"][index]
            if state != "OPEN":
                case.events = [*case.events, {"timestamp": iso(now), "actor": "Demo", "from_state": "OPEN", "to_state": state, "comment": "Estado ilustrativo del flujo de atención"}]
                case.state, case.version = state, 2
            if state in {"RESOLVED", "ACCEPTED"}:
                case.root_cause = "Incidencia identificada en el archivo de ejemplo."
                case.resolution = "Decisión documentada para ilustrar el cierre de una excepción demo."
        audit(db, "DEMO_SEED_COMPLETED", "system", "demo-v1", "Escenario demo preparado: Intake, Recon, Sentinel y excepciones.", "Demo")
        db.commit()


def main():
    Base.metadata.create_all(engine)
    seed_demo()
    print("Trackvance: esquema inicializado y demo disponible." if DEMO_ENABLED else "Trackvance: esquema inicializado; demo deshabilitada.")


if __name__ == "__main__":
    main()
