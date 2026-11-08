import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from app import probes


@pytest.fixture(autouse=True)
def fast_timeout():
    with patch("app.main.CHECK_TIMEOUT", 0.05):
        yield


def _wait_until_idle(key, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        entry = probes._inflight.get(key)
        if entry is None or entry[0].done():
            return
        time.sleep(0.01)
    raise AssertionError(f"probe {key} never finished")


def _redis_ok():
    redis = MagicMock()
    redis.ping.return_value = True
    redis.get.return_value = None
    return redis


class TestSingleFlightProbes:
    def _run_blocked_probe_test(self, client, probe_name, check_name, json_section):
        release = threading.Event()
        calls = []

        def blocked(*args, **kwargs):
            calls.append(1)
            release.wait(timeout=10)
            return None

        try:
            with patch(f"app.main.{probe_name}", blocked), patch("app.main.get_redis", return_value=_redis_ok()):
                results = [client.get("/_info").json() for _ in range(len(probes.PROBE_KEYS) * 3)]
                assert len(calls) == 1
                assert all(r["checks"]["redis-reachable"]["ok"] is True for r in results)
                if check_name:
                    assert results[0]["checks"][check_name]["ok"] is False
                    assert "in flight since" in results[-1]["checks"][check_name]["debug"]
                release.set()
                _wait_until_idle(json_section)
                client.get("/_info")
                assert len(calls) == 2
        finally:
            release.set()
            _wait_until_idle(json_section)

    def test_db_check_blocks_at_most_one_thread_and_redis_stays_ok(self, client):
        self._run_blocked_probe_test(client, "_probe_db_check", "db-reachable", "db-check")

    def test_db_metrics_blocks_at_most_one_thread_and_redis_stays_ok(self, client):
        self._run_blocked_probe_test(client, "_probe_db_metrics", None, "db-metrics")

    def test_recovers_once_blocked_call_returns(self, client):
        release = threading.Event()
        calls = []

        def blocked():
            calls.append(1)
            release.wait(timeout=10)

        with patch("app.main._probe_db_check", blocked), patch("app.main.get_redis", return_value=_redis_ok()):
            assert client.get("/_info").json()["checks"]["db-reachable"]["ok"] is False
            release.set()
            _wait_until_idle("db-check")
        assert client.get("/_info").json()["checks"]["db-reachable"]["ok"] is True

    def test_info_does_not_use_the_default_executor(self):
        import inspect
        import app.main
        assert "to_thread" not in inspect.getsource(app.main)


class TestJoinInFlight:
    def test_joiner_times_out_without_submitting_then_succeeds(self):
        import asyncio
        release = threading.Event()
        calls = []

        def blocked():
            calls.append(1)
            release.wait(timeout=10)
            return "done"

        async def scenario():
            first = asyncio.ensure_future(probes.run_probe("db-check", blocked, 0.05))
            await asyncio.sleep(0.01)
            with pytest.raises(asyncio.TimeoutError, match="in flight since"):
                await probes.run_probe("db-check", blocked, 0.05)
            assert len(calls) == 1
            with pytest.raises(asyncio.TimeoutError):
                await first
            joiner = asyncio.ensure_future(probes.run_probe("db-check", blocked, 5))
            await asyncio.sleep(0.01)
            release.set()
            assert await joiner == "done"
            assert len(calls) == 1

        try:
            asyncio.run(scenario())
        finally:
            release.set()
            _wait_until_idle("db-check")

    def test_overlapping_calls_on_healthy_probe_both_succeed(self):
        import asyncio

        def slowish():
            time.sleep(0.05)
            return "ok"

        async def scenario():
            return await asyncio.gather(
                probes.run_probe("db-check", slowish, 1),
                probes.run_probe("db-check", slowish, 1),
            )

        assert asyncio.run(scenario()) == ["ok", "ok"]
