"""Tests for refresh_contact_display_name in lucos_photos_common.jobs."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from lucos_photos_common.jobs import refresh_contact_display_name
from lucos_photos_common.models import Person


def _make_person(db_session, *, contact_id, display_name=None):
    person = Person(contact_id=contact_id, display_name=display_name)
    db_session.add(person)
    db_session.commit()
    db_session.refresh(person)
    return person


def _mock_httpx_get(name):
    def _get(url, **kwargs):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"name": name}
        return resp
    return _get


class TestRefreshContactDisplayName:
    def test_updates_display_name_when_changed(self, db_session, monkeypatch):
        """Fetches fresh name from contacts API and updates person in DB."""
        monkeypatch.setenv("LUCOS_CONTACTS_ORIGIN", "https://contacts.example.com")
        monkeypatch.setenv("KEY_LUCOS_CONTACTS", "testkey")
        person = _make_person(db_session, contact_id="contact-42", display_name="Old Name")

        with patch("httpx.get", side_effect=_mock_httpx_get("New Name")):
            refresh_contact_display_name("contact-42")

        db_session.refresh(person)
        assert person.display_name == "New Name"

    def test_no_update_when_name_unchanged(self, db_session, monkeypatch):
        """Does not commit when the name is already current."""
        monkeypatch.setenv("LUCOS_CONTACTS_ORIGIN", "https://contacts.example.com")
        monkeypatch.setenv("KEY_LUCOS_CONTACTS", "testkey")
        person = _make_person(db_session, contact_id="contact-42", display_name="Alice")

        with patch("httpx.get", side_effect=_mock_httpx_get("Alice")):
            refresh_contact_display_name("contact-42")

        db_session.refresh(person)
        assert person.display_name == "Alice"

    def test_no_op_when_contact_id_not_in_db(self, db_session, monkeypatch):
        """Silently returns if no person with that contact_id exists."""
        monkeypatch.setenv("LUCOS_CONTACTS_ORIGIN", "https://contacts.example.com")
        monkeypatch.setenv("KEY_LUCOS_CONTACTS", "testkey")

        with patch("httpx.get") as mock_get:
            refresh_contact_display_name("unknown-contact")
            mock_get.assert_not_called()

    def test_constructs_url_from_db_contact_id(self, db_session, monkeypatch):
        """HTTP request URL is built from LUCOS_CONTACTS_ORIGIN + person.contact_id, not from user input."""
        monkeypatch.setenv("LUCOS_CONTACTS_ORIGIN", "https://contacts.example.com")
        monkeypatch.setenv("KEY_LUCOS_CONTACTS", "testkey")
        _make_person(db_session, contact_id="contact-42", display_name="Old")

        captured = []

        def capture_url(url, **kwargs):
            captured.append(url)
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            resp.json.return_value = {"name": "New"}
            return resp

        with patch("httpx.get", side_effect=capture_url):
            refresh_contact_display_name("contact-42")

        assert captured == ["https://contacts.example.com/people/contact-42"]

    def test_skips_when_env_vars_unset(self, db_session, monkeypatch):
        """Returns early if LUCOS_CONTACTS_ORIGIN or KEY_LUCOS_CONTACTS are missing."""
        monkeypatch.delenv("LUCOS_CONTACTS_ORIGIN", raising=False)
        monkeypatch.delenv("KEY_LUCOS_CONTACTS", raising=False)
        _make_person(db_session, contact_id="contact-42", display_name="Old")

        with patch("httpx.get") as mock_get:
            refresh_contact_display_name("contact-42")
            mock_get.assert_not_called()

    def test_http_error_does_not_crash(self, db_session, monkeypatch):
        """A network failure is logged and swallowed — person record is not updated."""
        monkeypatch.setenv("LUCOS_CONTACTS_ORIGIN", "https://contacts.example.com")
        monkeypatch.setenv("KEY_LUCOS_CONTACTS", "testkey")
        person = _make_person(db_session, contact_id="contact-42", display_name="Old")

        def failing_get(url, **kwargs):
            raise httpx.ConnectError("unreachable")

        with patch("httpx.get", side_effect=failing_get):
            refresh_contact_display_name("contact-42")

        db_session.refresh(person)
        assert person.display_name == "Old"
