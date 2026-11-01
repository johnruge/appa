-- Least-privilege setup for one appa database. Run as a superuser with psql:
--   psql -v db=appa -v owner_pw="'...'" -v app_pw="'...'" -f roles.sql
--
-- appa_owner  owns the schema; used only to run migrations (DDL).
-- appa_app    what the running application connects as: read/write rows, no DDL.

\set ON_ERROR_STOP on

SELECT 'CREATE ROLE appa_owner LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'appa_owner') \gexec
SELECT 'CREATE ROLE appa_app LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'appa_app') \gexec
ALTER ROLE appa_owner WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :owner_pw;
ALTER ROLE appa_app   WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :app_pw;
ALTER ROLE appa_app SET statement_timeout = '10s';
ALTER ROLE appa_app SET idle_in_transaction_session_timeout = '30s';

SELECT format('CREATE DATABASE %I OWNER appa_owner', :'db')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db') \gexec

\connect :db
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO appa_owner, appa_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO appa_owner;
GRANT USAGE ON SCHEMA public TO appa_app;

-- Every table/sequence appa_owner creates (via migrations) is usable, not alterable, by appa_app.
ALTER DEFAULT PRIVILEGES FOR ROLE appa_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO appa_app;
ALTER DEFAULT PRIVILEGES FOR ROLE appa_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO appa_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO appa_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO appa_app;
