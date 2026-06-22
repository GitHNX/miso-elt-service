"""
CloudWatch custom metrics publisher.

Emits metrics for:
  - ingestion_success / ingestion_failure counts
  - rows_upserted gauge
  - last_successful_ingestion_age_seconds (for stale-data alerting)

In local/test environments (no SNS ARN), this silently no-ops.
"""
import time
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from src.core.config import get_settings
from src.core.logging import get_logger

logger = get_logger(__name__)


class MetricsPublisher:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._namespace = self._settings.cloudwatch_namespace
        self._enabled = bool(self._settings.sns_alert_topic_arn or self._settings.aws_region)

        if self._enabled:
            self._cw = boto3.client("cloudwatch", region_name=self._settings.aws_region)
            self._sns = boto3.client("sns", region_name=self._settings.aws_region)
        else:
            self._cw = None
            self._sns = None

    def record_ingestion_success(self, rows_upserted: int, interval_utc: datetime) -> None:
        self._put_metrics([
            {"MetricName": "IngestionSuccess", "Value": 1, "Unit": "Count"},
            {"MetricName": "RowsUpserted", "Value": rows_upserted, "Unit": "Count"},
            {
                "MetricName": "LastSuccessfulIngestionAgeSeconds",
                "Value": (datetime.now(timezone.utc) - interval_utc).total_seconds(),
                "Unit": "Seconds",
            },
        ])

    def record_ingestion_failure(self, error: str) -> None:
        self._put_metrics([
            {"MetricName": "IngestionFailure", "Value": 1, "Unit": "Count"},
        ])
        self._send_alert(
            subject="[MISO ELT] Ingestion Failure",
            message=f"Ingestion worker failed at {datetime.now(timezone.utc).isoformat()}\n\nError:\n{error}",
        )

    def record_api_latency_ms(self, latency_ms: float) -> None:
        self._put_metrics([
            {"MetricName": "MISOAPILatencyMs", "Value": latency_ms, "Unit": "Milliseconds"},
        ])

    def _put_metrics(self, metric_data: list[dict]) -> None:
        if not self._cw:
            logger.debug("cloudwatch_disabled_skipping_metrics", count=len(metric_data))
            return
        try:
            self._cw.put_metric_data(
                Namespace=self._namespace,
                MetricData=metric_data,
            )
        except (BotoCoreError, ClientError) as exc:
            # Metric failures must never crash the ingestion worker
            logger.warning("cloudwatch_put_metric_failed", error=str(exc))

    def _send_alert(self, subject: str, message: str) -> None:
        if not self._sns or not self._settings.sns_alert_topic_arn:
            logger.warning("sns_alert_skipped_no_arn", subject=subject)
            return
        try:
            self._sns.publish(
                TopicArn=self._settings.sns_alert_topic_arn,
                Subject=subject[:100],
                Message=message,
            )
            logger.info("sns_alert_sent", subject=subject)
        except (BotoCoreError, ClientError) as exc:
            logger.error("sns_alert_failed", error=str(exc))
