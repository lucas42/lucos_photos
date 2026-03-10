"""Tests for GET /api/app/latest and the /app downloads page."""

import pytest
import httpx
from unittest.mock import AsyncMock, patch, MagicMock
from pathlib import Path

import app.main as main_module

REAL_STATIC_DIR = Path(main_module.__file__).parent / "static"


@pytest.fixture(autouse=True)
def clear_app_latest_cache():
    """Reset the in-memory cache before each test to ensure isolation."""
    main_module._APP_LATEST_CACHE["data"] = None
    main_module._APP_LATEST_CACHE["fetched_at"] = 0.0
    yield
    main_module._APP_LATEST_CACHE["data"] = None
    main_module._APP_LATEST_CACHE["fetched_at"] = 0.0


def _make_github_response(status_code=200, json_body=None):
    """Build a mock httpx.Response for the GitHub Releases API."""
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = status_code
    mock_resp.is_success = (200 <= status_code < 300)
    if json_body is not None:
        mock_resp.json.return_value = json_body
    return mock_resp


SAMPLE_RELEASE = {
    "tag_name": "v1.2.3",
    "published_at": "2026-03-09T12:00:00Z",
    "assets": [
        {
            "name": "app-release.apk",
            "browser_download_url": "https://github.com/lucas42/lucos_photos_android/releases/download/v1.2.3/app-release.apk",
        }
    ],
}


class TestAppLatestAuth:
    """Authentication checks for GET /api/app/latest."""

    def test_unauthenticated_api_request_returns_401(self, client):
        response = client.get("/api/app/latest", headers={"Accept": "application/json"})
        assert response.status_code == 401

    def test_unauthenticated_browser_request_redirects_to_auth(self, client):
        response = client.get("/api/app/latest", headers={"Accept": "text/html"}, follow_redirects=False)
        assert response.status_code == 302
        assert "auth.l42.eu" in response.headers["location"]

    def test_authenticated_request_succeeds(self, authenticated_client):
        """Authenticated requests should not be blocked by auth (GitHub API call is mocked)."""
        mock_resp = _make_github_response(200, SAMPLE_RELEASE)
        with patch("app.main.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            response = authenticated_client.get("/api/app/latest")
        assert response.status_code == 200


class TestAppLatestSuccess:
    """Happy-path tests for GET /api/app/latest."""

    def test_returns_version_and_download_url(self, authenticated_client):
        mock_resp = _make_github_response(200, SAMPLE_RELEASE)
        with patch("app.main.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            response = authenticated_client.get("/api/app/latest")

        assert response.status_code == 200
        data = response.json()
        assert data["version"] == "1.2.3"
        assert data["download_url"] == "https://github.com/lucas42/lucos_photos_android/releases/download/v1.2.3/app-release.apk"
        assert data["released_at"] == "2026-03-09T12:00:00Z"

    def test_strips_leading_v_from_tag_name(self, authenticated_client):
        release = {**SAMPLE_RELEASE, "tag_name": "v2.0.0"}
        mock_resp = _make_github_response(200, release)
        with patch("app.main.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            response = authenticated_client.get("/api/app/latest")

        assert response.json()["version"] == "2.0.0"

    def test_caches_result_and_does_not_call_github_twice(self, authenticated_client):
        mock_resp = _make_github_response(200, SAMPLE_RELEASE)
        with patch("app.main.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            authenticated_client.get("/api/app/latest")
            authenticated_client.get("/api/app/latest")

        # GitHub API should only be called once (second response served from cache)
        assert mock_client.get.call_count == 1


class TestAppLatestErrors:
    """Error handling for GET /api/app/latest."""

    def test_returns_404_when_no_releases(self, authenticated_client):
        mock_resp = _make_github_response(404)
        with patch("app.main.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            response = authenticated_client.get("/api/app/latest")

        assert response.status_code == 404

    def test_returns_404_when_release_has_no_apk_asset(self, authenticated_client):
        release_without_apk = {
            "tag_name": "v1.0.0",
            "published_at": "2026-03-01T00:00:00Z",
            "assets": [
                {"name": "checksums.txt", "browser_download_url": "https://github.com/..."}
            ],
        }
        mock_resp = _make_github_response(200, release_without_apk)
        with patch("app.main.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            response = authenticated_client.get("/api/app/latest")

        assert response.status_code == 404

    def test_returns_502_when_github_api_unreachable(self, authenticated_client):
        with patch("app.main.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            response = authenticated_client.get("/api/app/latest")

        assert response.status_code == 502

    def test_returns_502_when_github_api_returns_server_error(self, authenticated_client):
        mock_resp = _make_github_response(500)
        with patch("app.main.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            response = authenticated_client.get("/api/app/latest")

        assert response.status_code == 502


class TestAppPage:
    """Tests for the /app HTML downloads page."""

    def test_requires_auth(self, client):
        response = client.get("/app", headers={"Accept": "application/json"})
        assert response.status_code == 401

    def test_browser_without_auth_redirects_to_auth(self, client):
        response = client.get("/app", headers={"Accept": "text/html"}, follow_redirects=False)
        assert response.status_code == 302
        assert "auth.l42.eu" in response.headers["location"]

    def test_authenticated_request_returns_200(self, authenticated_client, monkeypatch):
        monkeypatch.setattr(main_module, "STATIC_DIR", REAL_STATIC_DIR)
        response = authenticated_client.get("/app")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_page_includes_lucos_navbar(self, authenticated_client, monkeypatch):
        monkeypatch.setattr(main_module, "STATIC_DIR", REAL_STATIC_DIR)
        response = authenticated_client.get("/app")
        assert response.status_code == 200
        assert "lucos-navbar" in response.text

    def test_page_includes_download_link_element(self, authenticated_client, monkeypatch):
        monkeypatch.setattr(main_module, "STATIC_DIR", REAL_STATIC_DIR)
        response = authenticated_client.get("/app")
        assert response.status_code == 200
        assert "download-button" in response.text

    def test_page_calls_api_app_latest(self, authenticated_client, monkeypatch):
        monkeypatch.setattr(main_module, "STATIC_DIR", REAL_STATIC_DIR)
        response = authenticated_client.get("/app")
        assert response.status_code == 200
        assert "/api/app/latest" in response.text

    def test_page_includes_installation_instructions(self, authenticated_client, monkeypatch):
        monkeypatch.setattr(main_module, "STATIC_DIR", REAL_STATIC_DIR)
        response = authenticated_client.get("/app")
        assert response.status_code == 200
        assert "unknown sources" in response.text
        assert "Install the APK" in response.text
        assert "Grant media access" in response.text


class TestHomepageAppLink:
    """Verify the homepage links to the app downloads page."""

    def test_homepage_links_to_app_page(self, authenticated_client, monkeypatch):
        monkeypatch.setattr(main_module, "STATIC_DIR", REAL_STATIC_DIR)
        response = authenticated_client.get("/")
        assert response.status_code == 200
        assert 'href="/app"' in response.text
