"""
Unit tests for MISOClient._parse()

No network calls — tests the parsing logic in isolation.
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from src.ingestion.miso_client import MISOClient, MISOClientError, FuelMixSnapshot

EST = timezone(timedelta(hours=-5))

SAMPLE_RESPONSE = {
    "RefId": "21-Jun-2026 - Interval 02:10 EST",
    "TotalMW": "64739",
    "Fuel": {
        "Type": [
            {"INTERVALEST": "2026-06-21 2:10:00 AM", "CATEGORY": "Coal",           "ACT": "17926", "FUEL_CATEGORY": "Coal (17,926 MW)"},
            {"INTERVALEST": "2026-06-21 2:10:00 AM", "CATEGORY": "Natural Gas",    "ACT": "20373", "FUEL_CATEGORY": "Natural Gas (20,373 MW)"},
            {"INTERVALEST": "2026-06-21 2:10:00 AM", "CATEGORY": "Nuclear",        "ACT": "11115", "FUEL_CATEGORY": "Nuclear (11,115 MW)"},
            {"INTERVALEST": "2026-06-21 2:10:00 AM", "CATEGORY": "Wind",           "ACT": "8587",  "FUEL_CATEGORY": "Wind (8,587 MW)"},
            {"INTERVALEST": "2026-06-21 2:10:00 AM", "CATEGORY": "Solar",          "ACT": "-1",    "FUEL_CATEGORY": "Solar (-1 MW)"},
            {"INTERVALEST": "2026-06-21 2:10:00 AM", "CATEGORY": "Battery Storage","ACT": "-311",  "FUEL_CATEGORY": "Battery Storage (-311 MW)"},
            {"INTERVALEST": "2026-06-21 2:10:00 AM", "CATEGORY": "Other",          "ACT": "1063",  "FUEL_CATEGORY": "Other (1,063 MW)"},
            {"INTERVALEST": "2026-06-21 2:10:00 AM", "CATEGORY": "Imports",        "ACT": "5675",  "FUEL_CATEGORY": "Imports (5,675 MW)"},
        ]
    },
}


class TestMISOClientParse:
    def test_parse_ref_id(self):
        snap = MISOClient._parse(SAMPLE_RESPONSE)
        assert snap.ref_id == "21-Jun-2026 - Interval 02:10 EST"

    def test_parse_total_mw(self):
        snap = MISOClient._parse(SAMPLE_RESPONSE)
        assert snap.total_mw == 64739.0

    def test_parse_interval_utc_normalisation(self):
        """EST 2:10 AM → UTC 7:10 AM (EST = UTC-5)."""
        snap = MISOClient._parse(SAMPLE_RESPONSE)
        expected_utc = datetime(2026, 6, 21, 7, 10, 0, tzinfo=timezone.utc)
        assert snap.interval_utc == expected_utc

    def test_parse_interval_is_timezone_aware(self):
        snap = MISOClient._parse(SAMPLE_RESPONSE)
        assert snap.interval_utc.tzinfo is not None

    def test_parse_reading_count(self):
        snap = MISOClient._parse(SAMPLE_RESPONSE)
        assert len(snap.readings) == 8

    def test_parse_negative_act_mw(self):
        """Solar and Battery Storage can be negative — must parse correctly."""
        snap = MISOClient._parse(SAMPLE_RESPONSE)
        solar = next(r for r in snap.readings if r.category == "Solar")
        battery = next(r for r in snap.readings if r.category == "Battery Storage")
        assert solar.act_mw == -1.0
        assert battery.act_mw == -311.0

    def test_parse_all_categories_present(self):
        snap = MISOClient._parse(SAMPLE_RESPONSE)
        categories = {r.category for r in snap.readings}
        expected = {"Coal", "Natural Gas", "Nuclear", "Wind", "Solar", "Battery Storage", "Other", "Imports"}
        assert categories == expected

    def test_parse_missing_ref_id_raises(self):
        bad = {k: v for k, v in SAMPLE_RESPONSE.items() if k != "RefId"}
        with pytest.raises(MISOClientError, match="Failed to parse"):
            MISOClient._parse(bad)

    def test_parse_missing_fuel_key_raises(self):
        bad = {**SAMPLE_RESPONSE, "Fuel": {}}
        with pytest.raises(MISOClientError):
            MISOClient._parse(bad)

    def test_parse_malformed_timestamp_raises(self):
        bad = {
            **SAMPLE_RESPONSE,
            "Fuel": {
                "Type": [{**SAMPLE_RESPONSE["Fuel"]["Type"][0], "INTERVALEST": "not-a-date"}]
            },
        }
        with pytest.raises(MISOClientError):
            MISOClient._parse(bad)

    def test_parse_non_numeric_act_raises(self):
        ft = SAMPLE_RESPONSE["Fuel"]["Type"]
        bad_ft = [{**ft[0], "ACT": "N/A"}, *ft[1:]]
        bad = {**SAMPLE_RESPONSE, "Fuel": {"Type": bad_ft}}
        with pytest.raises(MISOClientError):
            MISOClient._parse(bad)


class TestRateLimitEnforcement:
    def test_rate_limit_enforces_minimum_interval(self):
        """_enforce_rate_limit must sleep if last fetch was < poll_interval ago."""
        import src.ingestion.miso_client as module
        import time

        with patch("src.ingestion.miso_client.get_settings") as mock_settings, \
             patch("src.ingestion.miso_client.time.sleep") as mock_sleep:

            mock_settings.return_value.miso_poll_interval_seconds = 60
            mock_settings.return_value.miso_request_timeout_seconds = 10
            mock_settings.return_value.miso_max_retries = 3

            module._last_fetch_ts = time.monotonic()   # pretend we just fetched

            client = MISOClient.__new__(MISOClient)
            client._settings = mock_settings.return_value
            client._enforce_rate_limit()

            mock_sleep.assert_called_once()
            sleep_duration = mock_sleep.call_args[0][0]
            assert 55 < sleep_duration <= 60
