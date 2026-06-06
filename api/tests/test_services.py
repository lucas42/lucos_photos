"""Real-transport tests for app/services.py.

Uses requests-mock (which intercepts at the HTTPAdapter level) rather than
mocking updateLoganne or the loganne module.  Tests will fail if:
  - a call omits `level` (arity error from the v2 client before HTTP is reached), or
  - `level` is not forwarded into the HTTP payload (regression in services.py).

conftest.py sets LOGANNE_ENDPOINT="http://loganne.test/events" before any
loganne module-level code runs, so all calls target that URL.
"""
import asyncio

import requests_mock as requests_mock_lib


class TestEmitLoganneEvent:
    """emit_loganne_event drives the real loganne client against a stubbed transport."""

    def test_posts_level_to_loganne_endpoint(self):
        """Default level 'routine' must appear in the POST payload."""
        from app.services import emit_loganne_event

        with requests_mock_lib.Mocker() as m:
            m.post("http://loganne.test/events", json={})
            asyncio.run(emit_loganne_event("photoAdded", "Photo 2024-01-01 added to lucos_photos"))

        assert m.called
        payload = m.last_request.json()
        assert payload["type"] == "photoAdded"
        assert payload["humanReadable"] == "Photo 2024-01-01 added to lucos_photos"
        assert payload["level"] == "routine"

    def test_custom_level_is_forwarded(self):
        """A caller-supplied level must reach the HTTP payload unchanged."""
        from app.services import emit_loganne_event

        with requests_mock_lib.Mocker() as m:
            m.post("http://loganne.test/events", json={})
            asyncio.run(emit_loganne_event("personCreated", "Person created", level="notable"))

        assert m.called
        payload = m.last_request.json()
        assert payload["level"] == "notable"

    def test_url_field_is_forwarded(self):
        """An optional url kwarg must appear in the HTTP payload."""
        from app.services import emit_loganne_event

        with requests_mock_lib.Mocker() as m:
            m.post("http://loganne.test/events", json={})
            asyncio.run(emit_loganne_event(
                "photoAdded",
                "Photo added",
                url="https://photos.example.com/photos/abc123",
            ))

        assert m.called
        payload = m.last_request.json()
        assert payload["url"] == "https://photos.example.com/photos/abc123"
