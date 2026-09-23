USE master;
GO
IF DB_ID(N'trackvance_delivery') IS NULL
    CREATE DATABASE [trackvance_delivery];
GO
IF NOT EXISTS (SELECT 1 FROM sys.sql_logins WHERE name = N'tv_delivery_writer')
    CREATE LOGIN tv_delivery_writer WITH PASSWORD = N'__WRITER_PASSWORD__', CHECK_POLICY = OFF;
ELSE
    ALTER LOGIN tv_delivery_writer WITH PASSWORD = N'__WRITER_PASSWORD__';
GO

USE trackvance_delivery;
GO
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'tv_delivery_writer')
    CREATE USER tv_delivery_writer FOR LOGIN tv_delivery_writer;
GO

IF SCHEMA_ID(N'existing_delivery') IS NULL
    EXEC(N'CREATE SCHEMA [existing_delivery] AUTHORIZATION [dbo]');
GO
IF OBJECT_ID(N'existing_delivery.records', N'U') IS NOT NULL
    DROP TABLE [existing_delivery].[records];
GO
CREATE TABLE [existing_delivery].[records] (
    [record_id] nvarchar(40) NOT NULL,
    [customer_name] nvarchar(160) NOT NULL,
    [amount] decimal(18, 2) NULL,
    [quantity] bigint NOT NULL,
    [happened_on] date NOT NULL,
    [updated_at] datetimeoffset(6) NOT NULL,
    [is_active] bit NOT NULL,
    [optional_note] nvarchar(240) NULL,
    CONSTRAINT [PK_delivery_records] PRIMARY KEY ([record_id])
);
GO
IF OBJECT_ID(N'existing_delivery.failing_records', N'U') IS NOT NULL
    DROP TABLE [existing_delivery].[failing_records];
GO
CREATE TABLE [existing_delivery].[failing_records] (
    [record_id] nvarchar(40) NOT NULL,
    [customer_name] nvarchar(160) NOT NULL,
    [amount] decimal(18, 2) NULL,
    [quantity] bigint NOT NULL,
    [happened_on] date NOT NULL,
    [updated_at] datetimeoffset(6) NOT NULL,
    [is_active] bit NOT NULL,
    [optional_note] nvarchar(240) NULL,
    CONSTRAINT [PK_delivery_failing_records] PRIMARY KEY ([record_id]),
    CONSTRAINT [CK_delivery_failing_quantity] CHECK ([quantity] > 100)
);
GO
GRANT SELECT, INSERT, UPDATE, DELETE, ALTER ON SCHEMA::[existing_delivery] TO [tv_delivery_writer];
GRANT CREATE TABLE TO [tv_delivery_writer];
GRANT CREATE SCHEMA TO [tv_delivery_writer];
GRANT VIEW DEFINITION TO [tv_delivery_writer];
GO

IF SCHEMA_ID(N'forbidden_delivery') IS NULL
    EXEC(N'CREATE SCHEMA [forbidden_delivery] AUTHORIZATION [dbo]');
GO
DENY SELECT, INSERT, UPDATE, DELETE ON SCHEMA::[forbidden_delivery] TO [tv_delivery_writer];
GO
