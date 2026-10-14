"""Real PostgreSQL 16/18 count matrix, called inside the disposable API image.

Only deterministic test records are used. Callers provide the ephemeral admin
password in memory; the returned report contains no connection details/secrets.
"""

from dataclasses import asdict

import psycopg

from trackvance.data_sinks import DeliveryError, DestinationSettings, PostgreSQLDataSink


def certify_postgres_metrics(host: str, password: str, port: int = 5432) -> dict:
    adapter = PostgreSQLDataSink(DestinationSettings(
        sink_type="POSTGRESQL", host=host, port=port, database="trackvance_delivery",
        username="delivery_admin", password=password, options={"sslmode": "disable"},
    ))
    results = []

    def execute(statement):
        with psycopg.connect(host=host, port=port, dbname="trackvance_delivery", user="delivery_admin",
                             password=password, sslmode="disable") as connection:
            cursor = connection.execute(statement)
            return cursor.fetchall() if statement.startswith("SELECT") else None

    # Schema name is scoped to the isolated runner; no operational DB is used.
    execute("CREATE SCHEMA metric_probe")
    execute("CREATE TABLE metric_probe.records (tenant text, key text, value text, "
            "CONSTRAINT records_key UNIQUE NULLS NOT DISTINCT (tenant,key), "
            "CONSTRAINT allowed_value CHECK (value IS DISTINCT FROM 'reject'))")
    columns = [{"source_name": name, "target_name": name, "target_type": "STRING",
                "ordinal": index, "nullable": True} for index, name in enumerate(
                    ("tenant", "key", "value"))]
    target = {"mode": "EXISTING_TABLE", "schema_name": "metric_probe",
              "table_name": "records", "_upsert_constraint": "records_key"}
    major = int(execute("SELECT current_setting('server_version_num')::int")[0][0]) // 10000

    def run_case(name, rows, expected, *, strategy="UPSERT", mapping=None):
        outcome = adapter.deliver(rows, mapping or columns, target, strategy,
                                  ["tenant", "key"] if strategy == "UPSERT" else [])
        assert (outcome.rows_inserted, outcome.rows_updated) == expected, name
        assert outcome.rows_attempted == outcome.rows_written == len(rows), name
        results.append({"case": name, "status": "PASS", "metrics": asdict(outcome)})

    def reliable(inserted, updated):
        return (inserted, updated) if major >= 18 else (None, None)

    run_case("all_insert_composite", [
        {"tenant": "A", "key": "1", "value": "first"},
        {"tenant": "B", "key": "1", "value": "second"}], reliable(2, 0))
    run_case("all_update_composite", [
        {"tenant": "A", "key": "1", "value": "updated"},
        {"tenant": "B", "key": "1", "value": "updated"}], reliable(0, 2))
    run_case("mixed_composite", [
        {"tenant": "A", "key": "1", "value": "mixed"},
        {"tenant": "A", "key": "2", "value": "new"}], reliable(1, 1))
    run_case("empty", [], (0, 0))
    run_case("key_only_do_nothing", [
        {"tenant": "A", "key": "1"}, {"tenant": "A", "key": "3"}], (1, 0),
        mapping=columns[:2])
    before = execute("SELECT tenant,key,value FROM metric_probe.records ORDER BY tenant,key")
    for name, changed_target, rows, error_code in [
        ("rollback", target, [{"tenant": "R", "key": "1", "value": "ok"},
                              {"tenant": "R", "key": "2", "value": "reject"}],
         "DESTINATION_CONSTRAINT_VIOLATION"),
        ("constraint_drift", {**target, "_upsert_constraint": "missing_key"},
         [{"tenant": "D", "key": "1", "value": "ok"}], "UPSERT_CONSTRAINT_DRIFT"),
    ]:
        try:
            adapter.deliver(rows, columns, changed_target, "UPSERT", ["tenant", "key"])
        except DeliveryError as error:
            assert error.code == error_code and not error.ambiguous, name
        else:
            raise AssertionError(name)
        assert execute("SELECT tenant,key,value FROM metric_probe.records ORDER BY tenant,key") == before
        results.append({"case": name, "status": "PASS", "error_code": error_code})
    execute("CREATE FUNCTION metric_probe.guard() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN IF NEW.value = 'skip' OR "
            "(TG_OP = 'UPDATE' AND NEW.value = 'skip_update') THEN RETURN NULL; END IF; "
            "IF NEW.key = 'nullify' THEN NEW.tenant := NULL; NEW.key := NULL; "
            "NEW.value := NULL; END IF; RETURN NEW; END $$")
    execute("CREATE TRIGGER guard BEFORE INSERT OR UPDATE ON metric_probe.records "
            "FOR EACH ROW EXECUTE FUNCTION metric_probe.guard()")
    run_case("trigger_suppressed_insert", [{"tenant": "T", "key": "1", "value": "skip"}], reliable(0, 0))
    run_case("trigger_suppressed_update", [{"tenant": "A", "key": "1", "value": "skip_update"}], reliable(0, 0))
    run_case("trigger_suppressed_append", [{"tenant": "T", "key": "1", "value": "skip"}],
             (0, 0), strategy="APPEND")
    execute("INSERT INTO metric_probe.records VALUES (NULL,NULL,NULL)")
    run_case("old_all_null_row_not_insert", [{"tenant": "T", "key": "nullify", "value": "v"}],
             reliable(0, 1))
    assert execute("SELECT count(*) FROM metric_probe.records WHERE key IS NULL")[0][0] == 1
    return {"engine": "POSTGRESQL", "major_version": major, "status": "PASS", "cases": results}
