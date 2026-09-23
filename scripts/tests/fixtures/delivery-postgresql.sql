DO $setup$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tv_delivery_writer') THEN
        CREATE ROLE tv_delivery_writer LOGIN PASSWORD '__WRITER_PASSWORD__';
    ELSE
        ALTER ROLE tv_delivery_writer PASSWORD '__WRITER_PASSWORD__';
    END IF;
END $setup$;

GRANT CONNECT, CREATE ON DATABASE trackvance_delivery TO tv_delivery_writer;
CREATE SCHEMA IF NOT EXISTS existing_delivery AUTHORIZATION delivery_admin;
GRANT USAGE, CREATE ON SCHEMA existing_delivery TO tv_delivery_writer;

DROP TABLE IF EXISTS existing_delivery.records;
CREATE TABLE existing_delivery.records (
    record_id varchar(40) PRIMARY KEY,
    customer_name varchar(160) NOT NULL,
    amount numeric(18, 2),
    quantity bigint NOT NULL,
    happened_on date NOT NULL,
    updated_at timestamp(6) with time zone NOT NULL,
    is_active boolean NOT NULL,
    optional_note varchar(240)
);
ALTER TABLE existing_delivery.records OWNER TO tv_delivery_writer;

DROP TABLE IF EXISTS existing_delivery.failing_records;
CREATE TABLE existing_delivery.failing_records (
    record_id varchar(40) PRIMARY KEY,
    customer_name varchar(160) NOT NULL,
    amount numeric(18, 2),
    quantity bigint NOT NULL CHECK (quantity > 100),
    happened_on date NOT NULL,
    updated_at timestamp(6) with time zone NOT NULL,
    is_active boolean NOT NULL,
    optional_note varchar(240)
);
ALTER TABLE existing_delivery.failing_records OWNER TO tv_delivery_writer;

CREATE SCHEMA IF NOT EXISTS forbidden_delivery AUTHORIZATION delivery_admin;
REVOKE ALL ON SCHEMA forbidden_delivery FROM PUBLIC;
REVOKE ALL ON SCHEMA forbidden_delivery FROM tv_delivery_writer;
