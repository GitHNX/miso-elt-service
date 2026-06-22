"""
Ingestion service — orchestrates fetch → transform → load.

The load step uses PostgreSQL's INSERT … ON CONFLICT DO UPDATE to make
every run idempotent: running the same interval multiple times is safe and
will only update act_mw / total_mw if they changed (MISO occasionally
revises values within the same interval).
"""
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.core.logging import get_logger
from src.db.session import get_app_session
from src.ingestion.miso_client import FuelMixSnapshot, MISOClient, MISOClientError
from src.ingestion.metrics import MetricsPublisher
from src.models.orm import DimFuelCategory, FactFuelMix, IngestionRun

logger = get_logger(__name__)


class IngestionService:
    def __init__(
        self,
        client: MISOClient | None = None,
        metrics: MetricsPublisher | None = None,
    ) -> None:
        self._client = client or MISOClient()
        self._metrics = metrics or MetricsPublisher()

    def run(self) -> None:
        """
        Execute one ingestion cycle:
        1. Fetch snapshot from MISO
        2. Upsert dim_fuel_category rows
        3. Upsert fact_fuel_mix rows
        4. Record audit row in ingestion_run
        5. Publish CloudWatch metrics
        """
        run_record = IngestionRun(started_at=datetime.now(timezone.utc), status="failure")

        try:
            snapshot = self._client.fetch()
            rows_upserted = self._load(snapshot, run_record)

            run_record.status = "success"
            run_record.rows_upserted = rows_upserted
            run_record.finished_at = datetime.now(timezone.utc)
            run_record.interval_est_utc = snapshot.interval_utc
            run_record.raw_ref_id = snapshot.ref_id

            self._metrics.record_ingestion_success(rows_upserted, snapshot.interval_utc)
            logger.info(
                "ingestion_complete",
                ref_id=snapshot.ref_id,
                rows_upserted=rows_upserted,
                interval_utc=snapshot.interval_utc.isoformat(),
            )

        except MISOClientError as exc:
            run_record.error_message = str(exc)
            run_record.finished_at = datetime.now(timezone.utc)
            self._metrics.record_ingestion_failure(str(exc))
            logger.error("ingestion_failed_miso_error", error=str(exc))
            raise

        except Exception as exc:
            run_record.error_message = str(exc)
            run_record.finished_at = datetime.now(timezone.utc)
            self._metrics.record_ingestion_failure(str(exc))
            logger.error("ingestion_failed_unexpected", error=str(exc), exc_info=True)
            raise

        finally:
            self._persist_run_record(run_record)

    def _load(self, snapshot: FuelMixSnapshot, run_record: IngestionRun) -> int:
        """Upsert dimension + fact rows. Returns number of fact rows upserted."""
        with get_app_session() as session:
            # ── Step 1: upsert dimension ──────────────────────────────────────
            category_ids: dict[str, int] = {}
            for reading in snapshot.readings:
                stmt = pg_insert(DimFuelCategory).values(
                    category_name=reading.category,
                    is_renewable=DimFuelCategory.is_renewable_category(reading.category),
                )
                stmt = stmt.on_conflict_do_nothing(index_elements=["category_name"])
                session.execute(stmt)

            # Flush so we can query the IDs (including newly inserted ones)
            session.flush()

            rows = session.execute(
                text("SELECT id, category_name FROM miso.dim_fuel_category")
            ).fetchall()
            category_ids = {r.category_name: r.id for r in rows}

            # ── Step 2: upsert facts ──────────────────────────────────────────
            fact_rows = [
                {
                    "interval_est_utc": snapshot.interval_utc,
                    "fuel_category_id": category_ids[reading.category],
                    "act_mw": reading.act_mw,
                    "total_mw": snapshot.total_mw,
                    "raw_ref_id": snapshot.ref_id,
                }
                for reading in snapshot.readings
                if reading.category in category_ids
            ]

            stmt = pg_insert(FactFuelMix).values(fact_rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["interval_est_utc", "fuel_category_id"],
                set_={
                    "act_mw": stmt.excluded.act_mw,
                    "total_mw": stmt.excluded.total_mw,
                    "raw_ref_id": stmt.excluded.raw_ref_id,
                    # ingested_at intentionally NOT updated — keep original write time
                },
            )
            result = session.execute(stmt)
            return result.rowcount

    def _persist_run_record(self, run_record: IngestionRun) -> None:
        """Write the audit row in a separate transaction so it always persists."""
        try:
            with get_app_session() as session:
                session.add(run_record)
        except Exception as exc:
            logger.error("failed_to_persist_run_record", error=str(exc))
