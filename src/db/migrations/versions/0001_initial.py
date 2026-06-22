"""Initial schema — miso star schema + read-only role

Revision ID: 0001_initial
Revises: 
Create Date: 2026-06-21
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Schema ────────────────────────────────────────────────────────────────
    #op.execute("CREATE SCHEMA IF NOT EXISTS miso")

    # ── dim_fuel_category ─────────────────────────────────────────────────────
    op.create_table(
        "dim_fuel_category",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("category_name", sa.String(100), nullable=False),
        sa.Column("is_renewable", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("category_name", name="uq_dim_fuel_category_name"),
        schema="miso",
    )

    # ── fact_fuel_mix ─────────────────────────────────────────────────────────
    op.create_table(
        "fact_fuel_mix",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("interval_est_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "fuel_category_id",
            sa.Integer(),
            sa.ForeignKey("miso.dim_fuel_category.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("act_mw", sa.Numeric(12, 2), nullable=False),
        sa.Column("total_mw", sa.Numeric(12, 2), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("raw_ref_id", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "interval_est_utc", "fuel_category_id",
            name="uq_fact_fuel_mix_interval_fuel",
        ),
        schema="miso",
    )
    op.create_index("ix_fact_fuel_mix_interval", "fact_fuel_mix", ["interval_est_utc"], schema="miso")
    op.create_index(
        "ix_fact_fuel_mix_fuel_interval",
        "fact_fuel_mix",
        ["fuel_category_id", "interval_est_utc"],
        schema="miso",
    )

    # ── ingestion_run ─────────────────────────────────────────────────────────
    op.create_table(
        "ingestion_run",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("rows_upserted", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("interval_est_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_ref_id", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('success', 'failure', 'skipped')", name="ck_ingestion_run_status"),
        schema="miso",
    )
    op.create_index("ix_ingestion_run_started", "ingestion_run", ["started_at"], schema="miso")

    # ── Read-only role (idempotent) ───────────────────────────────────────────
    # The role/user must be created by a superuser; here we issue it via
    # migration so it is tracked. In RDS the master user has CREATEROLE.
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'miso_readonly') THEN
                CREATE ROLE miso_readonly NOLOGIN;
            END IF;
        END
        $$;
    """)
    op.execute("GRANT USAGE ON SCHEMA miso TO miso_readonly")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA miso TO miso_readonly")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA miso GRANT SELECT ON TABLES TO miso_readonly")

    # The actual login user is created separately (password injected at deploy time)
    # See scripts/create_readonly_user.sql


def downgrade() -> None:
    op.drop_table("ingestion_run", schema="miso")
    op.drop_table("fact_fuel_mix", schema="miso")
    op.drop_table("dim_fuel_category", schema="miso")
    op.execute("DROP SCHEMA IF EXISTS miso CASCADE")
