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


class TestLoganneWebhook:
    def test_contactUpdated_extracts_contact_id_and_refreshes(self, client, db_session, monkeypatch):
        """contactUpdated with a valid URL triggers refresh_contact_display_name."""
        monkeypatch.setenv("LUCOS_CONTACTS_URL", "https://contacts.example.com")
        make_person(db_session, "Old Name", contact_id="contact-42")
        db_session.commit()

        with patch("lucos_photos_common.jobs.refresh_contact_display_name") as mock_refresh:
            response = client.post("/webhooks/loganne", json={
                "type": "contactUpdated",
                "source": "lucos_contacts",
                "url": CONTACT_URL,
            }, headers=VALID_AUTH)

        assert response.status_code == 204
        mock_refresh.assert_called_once_with("contact-42")

    def test_contactUpdated_url_validated_against_contacts_base(self, client, db_session, monkeypatch):
        """URL outside LUCOS_CONTACTS_URL is rejected before any refresh."""
        monkeypatch.setenv("LUCOS_CONTACTS_URL", "https://contacts.example.com")
        with patch("lucos_photos_common.jobs.refresh_contact_display_name") as mock_refresh:
            response = client.post("/webhooks/loganne", json={
                "type": "contactUpdated",
                "url": "https://attacker.example.com/steal",
            }, headers=VALID_AUTH)

        assert response.status_code == 204
        mock_refresh.assert_not_called()

    def test_contactUpdated_rejected_when_contacts_base_unset(self, client, db_session, monkeypatch):
        """When LUCOS_CONTACTS_URL is unset, all contactUpdated events are silently ignored."""
        monkeypatch.delenv("LUCOS_CONTACTS_URL", raising=False)
        with patch("lucos_photos_common.jobs.refresh_contact_display_name") as mock_refresh:
            response = client.post("/webhooks/loganne", json={
                "type": "contactUpdated",
                "url": CONTACT_URL,
            }, headers=VALID_AUTH)

        assert response.status_code == 204
        mock_refresh.assert_not_called()

    def test_missing_url_is_ignored(self, client, db_session, monkeypatch):
        """contactUpdated with no url field is silently ignored."""
        monkeypatch.setenv("LUCOS_CONTACTS_URL", "https://contacts.example.com")
        with patch("lucos_photos_common.jobs.refresh_contact_display_name") as mock_refresh:
            response = client.post("/webhooks/loganne", json={
                "type": "contactUpdated",
                "source": "lucos_contacts",
            }, headers=VALID_AUTH)

        assert response.status_code == 204
        mock_refresh.assert_not_called()

    def test_other_event_types_ignored(self, client, db_session, monkeypatch):
        monkeypatch.setenv("LUCOS_CONTACTS_URL", "https://contacts.example.com")
        with patch("lucos_photos_common.jobs.refresh_contact_display_name") as mock_refresh:
            response = client.post("/webhooks/loganne", json={
                "type": "contactCreated",
                "source": "lucos_contacts",
                "url": CONTACT_URL,
            }, headers=VALID_AUTH)

        assert response.status_code == 204
        mock_refresh.assert_not_called()

    def test_unauthenticated_rejected(self, client):
        """Requests without an Authorization header are rejected with 401."""
        with patch("lucos_photos_common.jobs.refresh_contact_display_name"):
            response = client.post("/webhooks/loganne", json={
                "type": "contactUpdated",
                "url": CONTACT_URL,
            })
        assert response.status_code == 401

    def test_invalid_token_rejected(self, client):
        """Requests with an invalid Bearer token are rejected with 401."""
        with patch("lucos_photos_common.jobs.refresh_contact_display_name"):
            response = client.post("/webhooks/loganne", json={
                "type": "contactUpdated",
                "url": CONTACT_URL,
            }, headers={"Authorization": "Bearer wrongtoken"})
        assert response.status_code == 401

    def test_valid_token_accepted(self, client, monkeypatch):
        """Requests with a valid Bearer token are accepted."""
        monkeypatch.setenv("LUCOS_CONTACTS_URL", "https://contacts.example.com")
        with patch("lucos_photos_common.jobs.refresh_contact_display_name") as mock_refresh:
            response = client.post("/webhooks/loganne", json={
                "type": "contactUpdated",
                "url": CONTACT_URL,
            }, headers={"Authorization": "Bearer validkey"})
        assert response.status_code == 204
        mock_refresh.assert_called_once_with("contact-42")
