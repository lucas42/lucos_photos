"""Unit tests for serialization helpers in app.serializers."""

from datetime import datetime, timezone

import pytest

from app.serializers import _format_taken_at, _ordinal


# ---------------------------------------------------------------------------
# _ordinal
# ---------------------------------------------------------------------------

class TestOrdinal:
    def test_first(self):
        assert _ordinal(1) == "1st"

    def test_second(self):
        assert _ordinal(2) == "2nd"

    def test_third(self):
        assert _ordinal(3) == "3rd"

    def test_fourth(self):
        assert _ordinal(4) == "4th"

    def test_eleventh(self):
        # 11th is the standard exception — not 11st
        assert _ordinal(11) == "11th"

    def test_twelfth(self):
        assert _ordinal(12) == "12th"

    def test_thirteenth(self):
        assert _ordinal(13) == "13th"

    def test_twenty_first(self):
        assert _ordinal(21) == "21st"

    def test_twenty_second(self):
        assert _ordinal(22) == "22nd"

    def test_twenty_third(self):
        assert _ordinal(23) == "23rd"

    def test_twenty_ninth(self):
        assert _ordinal(29) == "29th"

    def test_thirty_first(self):
        assert _ordinal(31) == "31st"


# ---------------------------------------------------------------------------
# _format_taken_at
# ---------------------------------------------------------------------------

class TestFormatTakenAt:
    def test_wednesday_example_from_issue(self):
        """Reproduces the example from issue #395: 29th April 2026 at 6:31pm."""
        dt = datetime(2026, 4, 29, 18, 31, 0, tzinfo=timezone.utc)
        assert _format_taken_at(dt) == "Weds 29th April 2026 at 6:31pm"

    def test_monday_with_morning_time(self):
        dt = datetime(2024, 1, 1, 9, 5, 0, tzinfo=timezone.utc)
        result = _format_taken_at(dt)
        assert result.startswith("Mon ")
        assert "1st January 2024" in result
        assert result.endswith("at 9:05am")

    def test_tuesday_with_noon(self):
        dt = datetime(2025, 6, 3, 12, 0, 0, tzinfo=timezone.utc)
        result = _format_taken_at(dt)
        assert result.startswith("Tues ")
        assert "3rd June 2025" in result
        assert result.endswith("at 12:00pm")

    def test_thursday_with_midnight(self):
        dt = datetime(2023, 11, 2, 0, 0, 0, tzinfo=timezone.utc)
        result = _format_taken_at(dt)
        assert result.startswith("Thurs ")
        assert "2nd November 2023" in result
        assert result.endswith("at 12:00am")

    def test_friday_with_afternoon_time(self):
        dt = datetime(2024, 3, 22, 15, 45, 0, tzinfo=timezone.utc)
        result = _format_taken_at(dt)
        assert result.startswith("Fri ")
        assert "22nd March 2024" in result
        assert result.endswith("at 3:45pm")

    def test_saturday(self):
        dt = datetime(2024, 2, 17, 8, 0, 0, tzinfo=timezone.utc)
        result = _format_taken_at(dt)
        assert result.startswith("Sat ")

    def test_sunday(self):
        dt = datetime(2024, 2, 18, 8, 0, 0, tzinfo=timezone.utc)
        result = _format_taken_at(dt)
        assert result.startswith("Sun ")

    def test_no_leading_zero_on_hour(self):
        """Single-digit hours must not have a leading zero."""
        dt = datetime(2024, 5, 10, 9, 30, 0, tzinfo=timezone.utc)
        result = _format_taken_at(dt)
        assert "at 9:30am" in result
        assert "at 09:30am" not in result

    def test_am_pm_lowercase(self):
        """am/pm must be lowercase."""
        am_dt = datetime(2024, 5, 10, 9, 0, 0, tzinfo=timezone.utc)
        pm_dt = datetime(2024, 5, 10, 21, 0, 0, tzinfo=timezone.utc)
        assert "am" in _format_taken_at(am_dt)
        assert "pm" in _format_taken_at(pm_dt)
        assert "AM" not in _format_taken_at(am_dt)
        assert "PM" not in _format_taken_at(pm_dt)
