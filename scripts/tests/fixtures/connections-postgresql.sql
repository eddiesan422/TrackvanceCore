CREATE SCHEMA source_data;
CREATE TABLE source_data.transactions (
    record_id varchar(20) NOT NULL,
    customer_name varchar(100),
    amount numeric(18,4),
    quantity integer,
    happened_on date,
    updated_at timestamp,
    is_active boolean,
    optional_note varchar(100)
);
INSERT INTO source_data.transactions VALUES
    ('001234567', 'José Álvarez', 10.2500, 1, '2024-01-01', '2024-01-01T10:30:00', true, '=not-a-formula'),
    ('1234567', 'María', -2.0000, 2, '2024-01-02', '2024-01-02T10:30:00', false, NULL),
    ('A-003', NULL, NULL, NULL, NULL, NULL, true, 'Unicode 東京'),
    ('A-004', 'Cliente 4', 0.0000, 0, '2024-01-04', '2024-01-04T10:30:00', true, ''),
    ('A-005', 'Cliente 5', 99.9900, 5, '2024-01-05', '2024-01-05T10:30:00', true, 'normal'),
    ('001234567', 'Cliente duplicado', 12.0000, 6, '2024-01-06', '2024-01-06T10:30:00', true, 'duplicado');
CREATE VIEW source_data.transactions_view AS SELECT * FROM source_data.transactions;
CREATE TABLE source_data.private_records (private_value varchar(20));
INSERT INTO source_data.private_records VALUES ('restricted');
CREATE ROLE tv_reader LOGIN PASSWORD '__READER_PASSWORD__';
GRANT CONNECT ON DATABASE trackvance_source TO tv_reader;
GRANT USAGE ON SCHEMA source_data TO tv_reader;
GRANT SELECT ON source_data.transactions, source_data.transactions_view TO tv_reader;
REVOKE ALL ON source_data.private_records FROM tv_reader;
ALTER ROLE tv_reader SET default_transaction_read_only = on;
