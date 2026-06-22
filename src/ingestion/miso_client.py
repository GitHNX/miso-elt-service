"""
MISO FuelMix API client.

Responsibilities
────────────────
- Fetch the current fuel-mix snapshot from MISO.
- Parse and validate the raw JSON into a typed dataclass.
- Enforce ≤1 call/minute via a module-level cooldown tracker.
- Retry transient HTTP/network errors with exponential back-off.
"""
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

from src.core.config import get_settings
from src.core.logging import get_logger

logger = get_logger(__name__)

# EST is UTC-5 (MISO reports in EST, not EDT — fixed offset)
_EST = timezone(timedelta(hours=-5))

# Module-level rate-limit guard
_last_fetch_ts: float = 0.0


@dataclass(frozen=True)
class FuelTypeReading:
    category: str
    act_mw: float


@dataclass(frozen=True)
class FuelMixSnapshot:
    ref_id: str
    interval_est: datetime          # Naive datetime in EST
    interval_utc: datetime          # UTC-normalised (timezone-aware)
    total_mw: float
    readings: list[FuelTypeReading] = field(default_factory=list)


class MISOClientError(Exception):
    """Raised for non-retryable MISO API errors."""


class MISOClient:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._http = httpx.Client(
            timeout=self._settings.miso_request_timeout_seconds,
            headers={"Accept": "application/json", "User-Agent": "miso-elt/1.0"},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def fetch(self) -> FuelMixSnapshot:
        """
        Fetch current fuel-mix snapshot.  Rate-limited to ≤1 call/minute.
        Raises MISOClientError on permanent failures.
        """
        self._enforce_rate_limit()
        return self._fetch_with_retry()

    def _enforce_rate_limit(self) -> None:
        global _last_fetch_ts
        min_interval = self._settings.miso_poll_interval_seconds
        elapsed = time.monotonic() - _last_fetch_ts
        if elapsed < min_interval:
            wait = min_interval - elapsed
            logger.info("rate_limit_sleep", wait_seconds=round(wait, 2))
            time.sleep(wait)

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        before_sleep=before_sleep_log(logger, "WARNING"),  # type: ignore[arg-type]
        reraise=True,
    )
    def _fetch_with_retry(self) -> FuelMixSnapshot:
        global _last_fetch_ts
        url = self._settings.miso_api_url

        logger.info("miso_api_request", url=url)
        response = self._http.get(url)

        _last_fetch_ts = time.monotonic()

        if response.status_code != 200:
            raise MISOClientError(
                f"MISO API returned HTTP {response.status_code}: {response.text[:200]}"
            )

        raw = response.json()
        snapshot = self._parse(raw)
        logger.info(
            "miso_api_response_parsed",
            ref_id=snapshot.ref_id,
            total_mw=snapshot.total_mw,
            fuel_types=len(snapshot.readings),
        )
        return snapshot

    @staticmethod
    def _parse(raw: dict) -> FuelMixSnapshot:
        """Parse raw API JSON into a typed snapshot."""
        try:
            ref_id: str = raw["RefId"]
            total_mw = float(raw["TotalMW"])
            fuel_types: list[dict] = raw["Fuel"]["Type"]

            # Parse interval from first fuel-type record
            # Format: "2026-06-21 2:10:00 AM"
            raw_ts: str = fuel_types[0]["INTERVALEST"]
            naive_est = datetime.strptime(raw_ts, "%Y-%m-%d %I:%M:%S %p")
            aware_est = naive_est.replace(tzinfo=_EST)
            utc_ts = aware_est.astimezone(timezone.utc)

            readings = [
                FuelTypeReading(
                    category=ft["CATEGORY"],
                    act_mw=float(ft["ACT"]),
                )
                for ft in fuel_types
            ]

            return FuelMixSnapshot(
                ref_id=ref_id,
                interval_est=naive_est,
                interval_utc=utc_ts,
                total_mw=total_mw,
                readings=readings,
            )
        except (KeyError, ValueError, IndexError) as exc:
            raise MISOClientError(f"Failed to parse MISO API response: {exc}") from exc
