-- Bootstrap database roles and databases.
-- Run with a PostgreSQL administrator and psql variables:
--
-- psql -d postgres -v ON_ERROR_STOP=1 \
--   -v control_password='...' -v runtime_password='...' \
--   -f migrations/bootstrap.sql
--
-- This file is intentionally separate from schema migrations because CREATE
-- DATABASE cannot run inside a transaction and requires an administrator.

\set ON_ERROR_STOP on
\if :{?control_user}
\else
\set control_user control
\endif
\if :{?control_db}
\else
\set control_db control
\endif
\if :{?runtime_user}
\else
\set runtime_user runtime
\endif
\if :{?runtime_db}
\else
\set runtime_db runtime
\endif
\if :{?control_password}
\else
\error 'control_password is required'
\quit 3
\endif
\if :{?runtime_password}
\else
\error 'runtime_password is required'
\quit 3
\endif

SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'control_user', :'control_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'control_user')\gexec
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'runtime_user', :'runtime_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'runtime_user')\gexec

SELECT format('CREATE DATABASE %I OWNER %I ENCODING ''UTF8''', :'control_db', :'control_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'control_db')\gexec
SELECT format('CREATE DATABASE %I OWNER %I ENCODING ''UTF8''', :'runtime_db', :'runtime_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'runtime_db')\gexec

\connect :control_db
CREATE EXTENSION IF NOT EXISTS pgcrypto;
GRANT CONNECT ON DATABASE :control_db TO :control_user;
GRANT USAGE, CREATE ON SCHEMA public TO :control_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :control_user;

\connect :runtime_db
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
GRANT CONNECT ON DATABASE :runtime_db TO :runtime_user;
GRANT USAGE, CREATE ON SCHEMA public TO :runtime_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :runtime_user;
