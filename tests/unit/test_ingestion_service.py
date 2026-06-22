"""
Unit tests for IngestionService.

Uses an in-memory SQLite engine to test the full upsert logic without
needing a live PostgreSQL instance.

Note: SQLite does not support ON CONFLICT DO UPDATE with the same syntax
as PostgreSQL, so these tests mock the session and verify call signatures.
The integration tests (tests/integration/) test against real Postgres.
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.ingestion.miso_client import FuelMixSnapshot, FuelTypeReading, MISOClientError
from src.ingestion.service import IngestionService
from src.models.orm import IngestionRun


def _make_snapshot(interval_utc=None) -> FuelMixSnapshot:
    interval_utc = interval_utc or datetime(2026, 6, 21, 7, 10, tzinfo=timezone.utc)
    return FuelMixSnapshot(
        ref_id="21-Jun-2026 - Interval 02:10 EST",
        interval_est=datetime(2026, 6, 21, 2, 10),
        interval_utc=interval_utc,
        total_mw=64739.0,
        readings=[
            FuelTypeReading(category="Coal", act_mw=17926.0),
            FuelTypeReading(category="Wind", act_mw=8587.0),
            FuelTypeReading(category="Solar", act_mw=-1.0),
        ],
    )


class TestIngestionServiceRunSuccess:
    def test_run_calls_fetch(self):
        mock_client = MagicMock()
        mock_client.fetch.return_value = _make_snapshot()
        mock_metrics = MagicMock()

        svc = IngestionService(client=mock_client, metrics=mock_metrics)

        with patch.object(svc, "_load", return_value=3), \
             patch.object(svc, "_persist_run_record"):
            svc.run()

        mock_client.fetch.assert_called_once()

    def test_run_records_success_metrics(self):
        mock_client = MagicMock()
        snapshot = _make_snapshot()
        mock_client.fetch.return_value = snapshot
        mock_metrics = MagicMock()

        svc = IngestionService(client=mock_client, metrics=mock_metrics)

        with patch.object(svc, "_load", return_value=3), \
             patch.object(svc, "_persist_run_record"):
            svc.run()

        mock_metrics.record_ingestion_success.assert_called_once_with(3, snapshot.interval_utc)
        mock_metrics.record_ingestion_failure.assert_not_called()

    def test_run_persists_run_record_on_success(self):
        mock_client = MagicMock()
        mock_client.fetch.return_value = _make_snapshot()
        mock_metrics = MagicMock()
        captured_records = []

        svc = IngestionService(client=mock_client, metrics=mock_metrics)

        with patch.object(svc, "_load", return_value=3), \
             patch.object(svc, "_persist_run_record", side_effect=captured_records.append):
            svc.run()

        assert len(captured_records) == 1
        record: IngestionRun = captured_records[0]
        assert record.status == "success"
        assert record.rows_upserted == 3
        assert record.error_message is None


class TestIngestionServiceRunFailure:
    def test_miso_client_error_sets_failure_status(self):
        mock_client = MagicMock()
        mock_client.fetch.side_effect = MISOClientError("API timeout")
        mock_metrics = MagicMock()
        captured_records = []

        svc = IngestionService(client=mock_client, metrics=mock_metrics)

        with patch.object(svc, "_persist_run_record", side_effect=captured_records.append):
            with pytest.raises(MISOClientError):
                svc.run()

        assert captured_records[0].status == "failure"
        assert "API timeout" in captured_records[0].error_message

    def test_failure_publishes_alert_metric(self):
        mock_client = MagicMock()
        mock_client.fetch.side_effect = MISOClientError("connection refused")
        mock_metrics = MagicMock()

        svc = IngestionService(client=mock_client, metrics=mock_metrics)

        with patch.object(svc, "_persist_run_record"):
            with pytest.raises(MISOClientError):
                svc.run()

        mock_metrics.record_ingestion_failure.assert_called_once()
        mock_metrics.record_ingestion_success.assert_not_called()

    def test_persist_run_record_always_called(self):
        """Audit record must be persisted even when ingestion raises."""
        mock_client = MagicMock()
        mock_client.fetch.side_effect = RuntimeError("unexpected error")
        mock_metrics = MagicMock()
        persist_called = []

        svc = IngestionService(client=mock_client, metrics=mock_metrics)

        with patch.object(svc, "_persist_run_record", side_effect=lambda r: persist_called.append(r)):
            with pytest.raises(RuntimeError):
                svc.run()

        assert len(persist_called) == 1  # always persisted in finally block


class TestIngestionIdempotency:
    """
    Verify that calling run() twice with the same snapshot does not raise
    and the second call is treated as an update (via the upsert).
    The actual SQL upsert is validated in integration tests.
    """
    def test_two_runs_same_interval_both_succeed(self):
        snapshot = _make_snapshot()
        mock_client = MagicMock()
        mock_client.fetch.return_value = snapshot
        mock_metrics = MagicMock()
        row_counts = [3, 3]  # both runs upsert the same 3 rows

        svc = IngestionService(client=mock_client, metrics=mock_metrics)

        with patch.object(svc, "_load", side_effect=row_counts), \
             patch.object(svc, "_persist_run_record"):
            svc.run()
            svc.run()   # should not raise

        assert mock_client.fetch.call_count == 2
