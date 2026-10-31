-- Application role: no superuser, no BYPASSRLS. Change the password.
CREATE ROLE billright_app LOGIN PASSWORD 'billright_app' NOSUPERUSER NOBYPASSRLS;
