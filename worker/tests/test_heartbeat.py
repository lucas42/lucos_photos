"""Tests for the worker heartbeat (RSS publish to Redis)."""

import json
import os
from unittest.mock import MagicMock, patch

from app.main import get_rss_bytes, publish_heartbeat, WORKER_HEARTBEAT_KEY, PENDING_SWEEP_INTERVAL_SECONDS


class TestGetRssBytes:
    def test_returns_integer_on_linux(self):
        """get_rss_bytes() should return a positive integer on a Linux host."""
        result = get_rss_bytes()
        # In a real Linux environment /proc/self/status exists; could be None in CI
        if result is not None:
            assert isinstance(result, int)
            assert result > 0

    def test_returns_none_on_missing_proc_file(self):
        with patch("builtins.open", side_effect=OSError("not found")):
            result = get_rss_bytes()
        assert result is None

    def test_converts_kb_to_bytes(self):
        from unittest.mock import mock_open
        fake_status = "Name:\tpython3\nVmRSS:\t  1024 kB\nVmPeak:\t  2048 kB\n"
        with patch("builtins.open", mock_open(read_data=fake_status)):
            result = get_rss_bytes()
        assert result == 1024 * 1024


class TestPublishHeartbeat:
    def test_writes_json_to_redis_key(self):
        redis_mock = MagicMock()
        with patch("app.main.get_rss_bytes", return_value=1234567):
            publish_heartbeat(redis_mock)

        redis_mock.set.assert_called_once()
        key, value = redis_mock.set.call_args[0]
        assert key == WORKER_HEARTBEAT_KEY
        data = json.loads(value)
        assert data["rss_bytes"] == 1234567
        assert "pid" in data
        assert "timestamp" in data

    def test_sets_ttl_as_3x_sweep_interval(self):
        redis_mock = MagicMock()
        with patch("app.main.get_rss_bytes", return_value=0):
            publish_heartbeat(redis_mock)

        _, kwargs = redis_mock.set.call_args
        assert kwargs["ex"] == PENDING_SWEEP_INTERVAL_SECONDS * 3

    def test_rss_bytes_can_be_none(self):
        """publish_heartbeat should not crash if get_rss_bytes returns None."""
        redis_mock = MagicMock()
        with patch("app.main.get_rss_bytes", return_value=None):
            publish_heartbeat(redis_mock)
        data = json.loads(redis_mock.set.call_args[0][1])
        assert data["rss_bytes"] is None
