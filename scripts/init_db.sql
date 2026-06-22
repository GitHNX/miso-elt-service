-- Runs once when the Postgres container is first initialized.
-- In RDS, this is handled by the Terraform null_resource / RDS init.

-- Create the read-only login user.
-- Password is overridden at deploy time via Secrets Manager.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'miso_readonly') THEN
        CREATE ROLE miso_readonly NOLOGIN;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'miso_readonly_user') THEN
        -- In local dev we use a static password; in RDS it's injected
        CREATE USER miso_readonly_user WITH PASSWORD 'localdevpassword' IN ROLE miso_readonly;
    END IF;
END
$$;
