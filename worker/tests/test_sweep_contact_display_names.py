"""Tests for sweep_contact_display_names in lucos_photos_common.jobs."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from lucos_photos_common.jobs import sweep_contact_display_names
from lucos_photos_common.models import Person


def _make_person(db_session, *, contact_id, display_name=None):
    person = Person(contact_id=contact_id, display_name=display_name)
    db_session.add(person)
    db_session.commit()
    db_session.refresh(person)
    return person


class TestSweepContactDisplayNames:

    def test_no_double_slash_when_url_has_trailing_slash(self, db_session, monkeypatch):
        """LUCOS_CONTACTS_URL with a trailing slash must not produce double-slash URLs."""
        monkeypatch.setenv("LUCOS_CONTACTS_URL", "https://contacts.l42.eu/")
        monkeypatch.setenv("KEY_LUCOS_CONTACTS", "testkey")

        person = _make_person(db_session, contact_id="42", display_name="Old Name")

        captured_urls = []

        def mock_get(url, **kwargs):
            captured_urls.append(url)
            mock_response = MagicMock()
            mock_response.raise_for_status.return_value = None
            mock_response.json.return_value = {"name": "Old Name"}
            return mock_response

        with patch("httpx.get", side_effect=mock_get):
            sweep_contact_display_names()

        assert len(captured_urls) == 1
        assert "//" not in captured_urls[0].replace("https://", "")

    def test_updates_display_name_when_different(self, db_session, monkeypatch):
        """display_name is updated when the contacts API returns a different name."""
        monkeypatch.setenv("LUCOS_CONTACTS_URL", "https://contacts.l42.eu/")
        monkeypatch.setenv("KEY_LUCOS_CONTACTS", "testkey")

        person = _make_person(db_session, contact_id="126", display_name="Old Name")

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"name": "New Name"}

        with patch("httpx.get", return_value=mock_response):
            sweep_contact_display_names()

        db_session.refresh(person)
        assert person.display_name == "New Name"

    def test_skips_when_name_unchanged(self, db_session, monkeypatch):
        """display_name is not updated when contacts API returns the same name."""
        monkeypatch.setenv("LUCOS_CONTACTS_URL", "https://contacts.l42.eu")
        monkeypatch.setenv("KEY_LUCOS_CONTACTS", "testkey")

        person = _make_person(db_session, contact_id="42", display_name="Same Name")

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"name": "Same Name"}

        with patch("httpx.get", return_value=mock_response):
            sweep_contact_display_names()

        db_session.refresh(person)
        assert person.display_name == "Same Name"

    def test_skips_persons_without_contact_id(self, db_session, monkeypatch):
        """Persons with no contact_id are not queried."""
        monkeypatch.setenv("LUCOS_CONTACTS_URL", "https://contacts.l42.eu")
        monkeypatch.setenv("KEY_LUCOS_CONTACTS", "testkey")

        _make_person(db_session, contact_id=None, display_name="No Contact")

        with patch("httpx.get") as mock_get:
            sweep_contact_display_names()

        mock_get.assert_not_called()

    def test_skips_entirely_when_url_not_set(self, db_session, monkeypatch):
        """Sweep is a no-op when LUCOS_CONTACTS_URL is not configured."""
        monkeypatch.delenv("LUCOS_CONTACTS_URL", raising=False)
        monkeypatch.setenv("KEY_LUCOS_CONTACTS", "testkey")

        _make_person(db_session, contact_id="1")

        with patch("httpx.get") as mock_get:
            sweep_contact_display_names()

        mock_get.assert_not_called()

    def test_continues_after_individual_fetch_failure(self, db_session, monkeypatch):
        """A 404 for one contact should not stop the sweep for others."""
        monkeypatch.setenv("LUCOS_CONTACTS_URL", "https://contacts.l42.eu")
        monkeypatch.setenv("KEY_LUCOS_CONTACTS", "testkey")

        person1 = _make_person(db_session, contact_id="1", display_name="Person One")
        person2 = _make_person(db_session, contact_id="2", display_name="Person Two")

        def side_effect(url, **kwargs):
            if "1" in url:
                raise httpx.HTTPStatusError(
                    "404", request=MagicMock(), response=MagicMock(status_code=404)
                )
            mock_response = MagicMock()
            mock_response.raise_for_status.return_value = None
            mock_response.json.return_value = {"name": "Updated Two"}
            return mock_response

        with patch("httpx.get", side_effect=side_effect):
            sweep_contact_display_names()

        db_session.refresh(person1)
        db_session.refresh(person2)
        assert person1.display_name == "Person One"  # unchanged — fetch failed
        assert person2.display_name == "Updated Two"
