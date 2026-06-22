"""
Star-schema data model for MISO fuel mix data.

Schema: miso

Dimension tables:
  dim_fuel_category   — slowly-changing list of fuel types (Coal, Wind, …)

Fact table:
  fact_fuel_mix       — one row per (interval_est, fuel_category_id)

Design notes
────────────
- Natural key for idempotency: (interval_est, fuel_category_id).
  INSERT … ON CONFLICT DO UPDATE makes every ingestion run idempotent.

- interval_est is stored as TIMESTAMPTZ in UTC.  The raw API value is
  "2026-06-21 2:10:00 AM" EST (UTC-5), so we always normalise on ingest.

- total_mw on the fact table is denormalised from the RefId envelope for
  convenience; it is the grid-wide total across all fuel types at that
  interval, NOT the per-fuel value.

- act_mw can be negative (Battery Storage charging, Solar at night are
  returned as -1 / negative values by MISO).
"""
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    __table_args__ = {"schema": "miso"}


# ── Dimension ─────────────────────────────────────────────────────────────────

class DimFuelCategory(Base):
    """
    Dimension: fuel category (Coal, Natural Gas, Nuclear, Wind, …).
    Rows are inserted on first encounter and never deleted.
    """
    __tablename__ = "dim_fuel_category"

    id = Column(Integer, primary_key=True, autoincrement=True)
    category_name = Column(String(100), nullable=False, unique=True)
    is_renewable = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    readings = relationship("FactFuelMix", back_populates="fuel_category")

    # Simple heuristic — can be overridden by an operator update
    RENEWABLES = frozenset({"Wind", "Solar", "Hydro"})

    @classmethod
    def is_renewable_category(cls, name: str) -> bool:
        return name in cls.RENEWABLES

    def __repr__(self) -> str:
        return f"<DimFuelCategory id={self.id} name={self.category_name!r}>"


# ── Fact ──────────────────────────────────────────────────────────────────────

class FactFuelMix(Base):
    """
    Fact: MW generation per fuel type per 5-minute MISO reporting interval.

    Natural key   : (interval_est_utc, fuel_category_id)
    Surrogate key : id  (bigint, for FK ergonomics)
    """
    __tablename__ = "fact_fuel_mix"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # MISO reports in EST; we persist in UTC to avoid DST ambiguity
    interval_est_utc = Column(
        DateTime(timezone=True),
        nullable=False,
        comment="Interval timestamp normalised to UTC",
    )
    fuel_category_id = Column(
        Integer,
        ForeignKey("miso.dim_fuel_category.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Per-fuel generation in MW (can be negative)
    act_mw = Column(Numeric(12, 2), nullable=False)

    # Grid-wide total from the API envelope (same for all rows in a batch)
    total_mw = Column(Numeric(12, 2), nullable=True)

    # Audit columns
    ingested_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Wall-clock time this row was written by the ingestion worker",
    )
    raw_ref_id = Column(
        Text,
        nullable=True,
        comment="RefId string from the MISO API envelope, e.g. '21-Jun-2026 - Interval 02:10 EST'",
    )

    fuel_category = relationship("DimFuelCategory", back_populates="readings")

    __table_args__ = (
        UniqueConstraint(
            "interval_est_utc",
            "fuel_category_id",
            name="uq_fact_fuel_mix_interval_fuel",
        ),
        # Partition-friendly index for time-range queries
        Index("ix_fact_fuel_mix_interval", "interval_est_utc"),
        Index("ix_fact_fuel_mix_fuel_interval", "fuel_category_id", "interval_est_utc"),
        {"schema": "miso"},
    )

    def __repr__(self) -> str:
        return (
            f"<FactFuelMix id={self.id} "
            f"interval={self.interval_est_utc.isoformat()} "
            f"fuel_category_id={self.fuel_category_id} "
            f"act_mw={self.act_mw}>"
        )


# ── Ingestion audit log ───────────────────────────────────────────────────────

class IngestionRun(Base):
    """
    One row per ingestion execution — success or failure.
    Used for operational dashboards and alerting on stale data.
    """
    __tablename__ = "ingestion_run"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(20), nullable=False)   # 'success' | 'failure' | 'skipped'
    rows_upserted = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    interval_est_utc = Column(DateTime(timezone=True), nullable=True)
    raw_ref_id = Column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('success', 'failure', 'skipped')", name="ck_ingestion_run_status"),
        Index("ix_ingestion_run_started", "started_at"),
        {"schema": "miso"},
    )
