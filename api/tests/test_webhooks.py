import pytest
from unittest.mock import AsyncMock, patch
from lucos_photos_common.models import Person


def make_person(db, display_name="Alice", contact_id=None):
    person = Person(display_name=display_name, contact_id=contact_id)
    db.add(person)
    db.flush()
    return person


class TestLoganneWebhook:
    def test_contactUpdated_syncs_display_name(self, client, db_session):
        person = make_person(db_session, "Old Name", contact_id="contact-42")
        db_session.commit()

        with patch("lucos_photos_common.jobs.sync_single_contact_name") as mock_sync:
            response = client.post("/webhooks/loganne", json={
                "type": "contactUpdated",
                "source": "lucos_contacts",
                "humanReadable": "Contact \"New Name\" updated",
                "agent": {"id": "contact-42", "name": "New Name"},
            })

        assert response.status_code == 204
        mock_sync.assert_called_once_with("contact-42", "New Name")

    def test_other_event_types_ignored(self, client, db_session):
        with patch("lucos_photos_common.jobs.sync_single_contact_name") as mock_sync:
            response = client.post("/webhooks/loganne", json={
                "type": "contactCreated",
                "source": "lucos_contacts",
                "humanReadable": "Contact \"Alice\" created",
                "agent": {"id": "contact-42", "name": "Alice"},
            })

        assert response.status_code == 204
        mock_sync.assert_not_called()

    def test_missing_agent_id_ignored(self, client):
        with patch("lucos_photos_common.jobs.sync_single_contact_name") as mock_sync:
            response = client.post("/webhooks/loganne", json={
                "type": "contactUpdated",
                "agent": {"name": "Alice"},
            })

        assert response.status_code == 204
        mock_sync.assert_not_called()

    def test_missing_agent_name_ignored(self, client):
        with patch("lucos_photos_common.jobs.sync_single_contact_name") as mock_sync:
            response = client.post("/webhooks/loganne", json={
                "type": "contactUpdated",
                "agent": {"id": "contact-42"},
            })

        assert response.status_code == 204
        mock_sync.assert_not_called()

    def test_no_auth_accepted_during_migration(self, client):
        """Phase 1: requests without an Authorization header are still accepted."""
        with patch("lucos_photos_common.jobs.sync_single_contact_name"):
            response = client.post("/webhooks/loganne", json={
                "type": "contactUpdated",
                "agent": {"id": "contact-42", "name": "Alice"},
            })
        assert response.status_code == 204

    def test_invalid_token_rejected(self, client):
        """Phase 1: requests with an invalid Bearer token are rejected with 401."""
        with patch("lucos_photos_common.jobs.sync_single_contact_name"):
            response = client.post("/webhooks/loganne", json={
                "type": "contactUpdated",
                "agent": {"id": "contact-42", "name": "Alice"},
            }, headers={"Authorization": "Bearer wrongtoken"})
        assert response.status_code == 401

    def test_valid_token_accepted(self, client):
        """Phase 1: requests with a valid Bearer token are accepted."""
        with patch("lucos_photos_common.jobs.sync_single_contact_name"):
            response = client.post("/webhooks/loganne", json={
                "type": "contactUpdated",
                "agent": {"id": "contact-42", "name": "Alice"},
            }, headers={"Authorization": "Bearer validkey"})
        assert response.status_code == 204
