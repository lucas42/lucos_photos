import pytest
from unittest.mock import AsyncMock, patch
from lucos_photos_common.models import Person


def make_person(db, display_name="Alice", contact_id=None):
    person = Person(display_name=display_name, contact_id=contact_id)
    db.add(person)
    db.flush()
    return person


VALID_AUTH = {"Authorization": "Bearer validkey"}

CONTACT_URL = "https://contacts.example.com/people/contact-42"


def _mock_fetch_contact(contact_id="contact-42", name="New Name"):
    """Return an AsyncMock that simulates a successful _fetch_contact call."""
    return AsyncMock(return_value=(contact_id, name))


class TestLoganneWebhook:
    def test_contactUpdated_fetches_url_and_syncs_display_name(self, client, db_session):
        """contactUpdated with a url field fetches the contact and syncs display_name."""
        make_person(db_session, "Old Name", contact_id="contact-42")
        db_session.commit()

        with patch("app.routers.webhooks._fetch_contact", _mock_fetch_contact()):
            with patch("lucos_photos_common.jobs.sync_single_contact_name") as mock_sync:
                response = client.post("/webhooks/loganne", json={
                    "type": "contactUpdated",
                    "source": "lucos_contacts",
                    "url": CONTACT_URL,
                    "humanReadable": "Contact \"New Name\" updated",
                }, headers=VALID_AUTH)

        assert response.status_code == 204
        mock_sync.assert_called_once_with("contact-42", "New Name")

    def test_contactUpdated_passes_url_to_fetch(self, client, db_session):
        """The url field from the event is passed to _fetch_contact."""
        fetch_mock = _mock_fetch_contact()
        with patch("app.routers.webhooks._fetch_contact", fetch_mock):
            with patch("lucos_photos_common.jobs.sync_single_contact_name"):
                client.post("/webhooks/loganne", json={
                    "type": "contactUpdated",
                    "url": CONTACT_URL,
                }, headers=VALID_AUTH)
        fetch_mock.assert_awaited_once_with(CONTACT_URL)

    def test_missing_url_is_ignored(self, client, db_session):
        """contactUpdated with no url field is silently ignored."""
        with patch("app.routers.webhooks._fetch_contact") as fetch_mock:
            with patch("lucos_photos_common.jobs.sync_single_contact_name") as mock_sync:
                response = client.post("/webhooks/loganne", json={
                    "type": "contactUpdated",
                    "source": "lucos_contacts",
                }, headers=VALID_AUTH)

        assert response.status_code == 204
        fetch_mock.assert_not_called()
        mock_sync.assert_not_called()

    def test_fetch_failure_is_ignored(self, client, db_session):
        """If _fetch_contact returns None (network failure, bad response), no sync is attempted."""
        with patch("app.routers.webhooks._fetch_contact", AsyncMock(return_value=None)):
            with patch("lucos_photos_common.jobs.sync_single_contact_name") as mock_sync:
                response = client.post("/webhooks/loganne", json={
                    "type": "contactUpdated",
                    "url": CONTACT_URL,
                }, headers=VALID_AUTH)

        assert response.status_code == 204
        mock_sync.assert_not_called()

    def test_other_event_types_ignored(self, client, db_session):
        with patch("app.routers.webhooks._fetch_contact") as fetch_mock:
            with patch("lucos_photos_common.jobs.sync_single_contact_name") as mock_sync:
                response = client.post("/webhooks/loganne", json={
                    "type": "contactCreated",
                    "source": "lucos_contacts",
                    "url": CONTACT_URL,
                }, headers=VALID_AUTH)

        assert response.status_code == 204
        fetch_mock.assert_not_called()
        mock_sync.assert_not_called()

    def test_unauthenticated_rejected(self, client):
        """Requests without an Authorization header are rejected with 401."""
        with patch("lucos_photos_common.jobs.sync_single_contact_name"):
            response = client.post("/webhooks/loganne", json={
                "type": "contactUpdated",
                "url": CONTACT_URL,
            })
        assert response.status_code == 401

    def test_invalid_token_rejected(self, client):
        """Requests with an invalid Bearer token are rejected with 401."""
        with patch("lucos_photos_common.jobs.sync_single_contact_name"):
            response = client.post("/webhooks/loganne", json={
                "type": "contactUpdated",
                "url": CONTACT_URL,
            }, headers={"Authorization": "Bearer wrongtoken"})
        assert response.status_code == 401

    def test_valid_token_accepted(self, client):
        """Requests with a valid Bearer token are accepted."""
        with patch("app.routers.webhooks._fetch_contact", _mock_fetch_contact()):
            with patch("lucos_photos_common.jobs.sync_single_contact_name") as mock_sync:
                response = client.post("/webhooks/loganne", json={
                    "type": "contactUpdated",
                    "url": CONTACT_URL,
                }, headers={"Authorization": "Bearer validkey"})
        assert response.status_code == 204
        mock_sync.assert_called_once()


class TestFetchContact:
    """Unit tests for the _fetch_contact helper."""

    from app.routers.webhooks import _fetch_contact

    def _mock_httpx_response(self, json_data, status_code=200):
        import httpx
        from unittest.mock import MagicMock
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = json_data
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        return mock_client

    def test_extracts_contact_id_from_url_path(self, client, monkeypatch):
        import asyncio
        from app.routers.webhooks import _fetch_contact
        monkeypatch.setenv("KEY_LUCOS_CONTACTS", "testkey")
        mock_client = self._mock_httpx_response({"name": "Alice"})
        with patch("app.routers.webhooks.httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(
                _fetch_contact("https://contacts.example.com/people/contact-99")
            )
        assert result is not None
        contact_id, name = result
        assert contact_id == "contact-99"
        assert name == "Alice"

    def test_returns_none_on_http_error(self, client, monkeypatch):
        import asyncio
        import httpx
        from unittest.mock import MagicMock
        from app.routers.webhooks import _fetch_contact
        monkeypatch.setenv("KEY_LUCOS_CONTACTS", "testkey")
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Not Found", request=MagicMock(), response=MagicMock(status_code=404)
        )
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch("app.routers.webhooks.httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(
                _fetch_contact("https://contacts.example.com/people/contact-99")
            )
        assert result is None

    def test_returns_none_when_name_missing(self, client, monkeypatch):
        import asyncio
        from app.routers.webhooks import _fetch_contact
        monkeypatch.setenv("KEY_LUCOS_CONTACTS", "testkey")
        mock_client = self._mock_httpx_response({"id": "contact-99"})
        with patch("app.routers.webhooks.httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(
                _fetch_contact("https://contacts.example.com/people/contact-99")
            )
        assert result is None
