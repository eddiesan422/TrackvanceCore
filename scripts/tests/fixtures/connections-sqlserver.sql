CREATE DATABASE trackvance_source;
GO
USE trackvance_source;
GO
CREATE SCHEMA source_data;
GO
CREATE TABLE source_data.transactions (
    record_id nvarchar(20) NOT NULL,
    customer_name nvarchar(100),
    amount decimal(18,4),
    quantity int,
    happened_on date,
    updated_at datetime2,
    is_active bit,
    optional_note nvarchar(100)
);
INSERT INTO source_data.transactions VALUES
    ('001234567', N'José Álvarez', 10.2500, 1, '2024-01-01', '2024-01-01T10:30:00', 1, '=not-a-formula'),
    ('1234567', N'María', -2.0000, 2, '2024-01-02', '2024-01-02T10:30:00', 0, NULL),
    ('A-003', NULL, NULL, NULL, NULL, NULL, 1, N'Unicode 東京'),
    ('A-004', 'Cliente 4', 0.0000, 0, '2024-01-04', '2024-01-04T10:30:00', 1, ''),
    ('A-005', 'Cliente 5', 99.9900, 5, '2024-01-05', '2024-01-05T10:30:00', 1, 'normal'),
    ('001234567', 'Cliente duplicado', 12.0000, 6, '2024-01-06', '2024-01-06T10:30:00', 1, 'duplicado');
GO
CREATE VIEW source_data.transactions_view AS SELECT * FROM source_data.transactions;
GO
CREATE TABLE source_data.private_records (private_value nvarchar(20));
INSERT INTO source_data.private_records VALUES ('restricted');
CREATE LOGIN tv_reader WITH PASSWORD = '__READER_PASSWORD__', CHECK_POLICY = OFF;
CREATE USER tv_reader FOR LOGIN tv_reader;
GRANT SELECT ON source_data.transactions TO tv_reader;
GRANT SELECT ON source_data.transactions_view TO tv_reader;
DENY SELECT ON source_data.private_records TO tv_reader;
DENY INSERT, UPDATE, DELETE ON SCHEMA::source_data TO tv_reader;
GO
