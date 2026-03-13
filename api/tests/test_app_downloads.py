"""Tests for GET /api/app/latest and the /app downloads page."""

import pytest
import httpx
from unittest.mock import AsyncMock, patch, MagicMock
from pathlib import Path

import app.main as main_module
import app.routers.app_release as app_release_module

REAL_STATIC_DIR = Path(main_module.__file__).parent / "static"


@pytest.fixture(autouse=True)
def clear_app_latest_cache():
    """Reset the in-memory caches before each test to ensure isolation."""
    app_release_module._APP_LATEST_CACHE["data"] = None
    app_release_module._APP_LATEST_CACHE["fetched_at"] = 0.0
    app_release_module._APP_LATEST_ERROR_CACHE["error"] = None
    app_release_module._APP_LATEST_ERROR_CACHE["fetched_at"] = 0.0
    yield
    app_release_module._APP_LATEST_CACHE["data"] = None
    app_release_module._APP_LATEST_CACHE["fetched_at"] = 0.0
    app_release_module._APP_LATEST_ERROR_CACHE["error"] = None
    app_release_module._APP_LATEST_ERROR_CACHE["fetched_at"] = 0.0


def _make_github_response(status_code=200, json_body=None):
    """Build a mock httpx.Response for the GitHub Releases API."""
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = status_code
    mock_resp.is_success = (200 <= status_code < 300)
    if json_body is not None:
        mock_resp.json.return_value = json_body
    return mock_resp


def _make_async_client(response):
    """Build a mock AsyncClient context manager that returns the given response."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


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
        with patch("app.routers.app_release.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            response = authenticated_client.get("/api/app/latest")
        assert response.status_code == 200

    def test_key_auth_bearer_scheme_succeeds(self, client):
        """Requests with a valid key via Bearer scheme should be accepted."""
        mock_resp = _make_github_response(200, SAMPLE_RELEASE)
        with patch("app.routers.app_release.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            response = client.get("/api/app/latest", headers={"Authorization": "Bearer validkey"})
        assert response.status_code == 200

    def test_key_auth_key_scheme_succeeds(self, client):
        """Requests with a valid key via 'key' scheme (Android app style) should be accepted."""
        mock_resp = _make_github_response(200, SAMPLE_RELEASE)
        with patch("app.routers.app_release.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            response = client.get("/api/app/latest", headers={"Authorization": "key validkey"})
        assert response.status_code == 200

    def test_invalid_key_returns_401(self, client):
        """Requests with an invalid key and no session cookie should be rejected."""
        response = client.get(
            "/api/app/latest",
            headers={"Authorization": "key wrongkey", "Accept": "application/json"},
        )
        assert response.status_code == 401

    def test_invalid_key_with_valid_session_cookie_succeeds(self, client):
        """An invalid key falls through to session cookie validation.

        This is deliberate: a browser user who happens to have an unrelated or
        stale Authorization header set (e.g. from a dev tool) should still be
        authenticated via their session cookie rather than being locked out.
        """
        mock_resp = _make_github_response(200, SAMPLE_RELEASE)

        # Mock the auth service to validate the session cookie
        mock_auth_resp = MagicMock()
        mock_auth_resp.raise_for_status.return_value = None
        mock_auth_resp.json.return_value = {"id": 1}

        mock_auth_client = AsyncMock()
        mock_auth_client.__aenter__ = AsyncMock(return_value=mock_auth_client)
        mock_auth_client.__aexit__ = AsyncMock(return_value=False)

        # The session verification calls httpx.AsyncClient for the auth service,
        # then the endpoint itself calls it for the GitHub API. We need to return
        # the right mock for each call.
        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if "auth.l42.eu" in url:
                return mock_auth_resp
            return mock_resp

        mock_auth_client.get = mock_get

        with patch("app.auth.httpx.AsyncClient", return_value=mock_auth_client), \
             patch("app.routers.app_release.httpx.AsyncClient", return_value=mock_auth_client):
            response = client.get(
                "/api/app/latest",
                headers={"Authorization": "key wrongkey", "Accept": "application/json"},
                cookies={"auth_token": "valid-session-token"},
            )

        assert response.status_code == 200


class TestAppLatestSuccess:
    """Happy-path tests for GET /api/app/latest."""

    def test_returns_version_and_download_url(self, authenticated_client):
        mock_resp = _make_github_response(200, SAMPLE_RELEASE)
        with patch("app.routers.app_release.httpx.AsyncClient") as mock_client_cls:
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
        with patch("app.routers.app_release.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            response = authenticated_client.get("/api/app/latest")

        assert response.json()["version"] == "2.0.0"

    def test_caches_result_and_does_not_call_github_twice(self, authenticated_client):
        mock_resp = _make_github_response(200, SAMPLE_RELEASE)
        with patch("app.routers.app_release.httpx.AsyncClient") as mock_client_cls:
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
        with patch("app.routers.app_release.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            response = authenticated_client.get("/api/app/latest")

        assert response.status_code == 404

    def test_returns_404_when_no_releases_have_apk(self, authenticated_client):
        """When the latest release has no APK, the fallback list is also checked.

        If no release in the list has an APK either, a 404 is returned.
        """
        release_without_apk = {
            "tag_name": "v1.0.0",
            "published_at": "2026-03-01T00:00:00Z",
            "assets": [
                {"name": "checksums.txt", "browser_download_url": "https://github.com/..."}
            ],
        }
        latest_resp = _make_github_response(200, release_without_apk)
        list_resp = _make_github_response(200, [release_without_apk])

        # Two separate AsyncClient context managers are used (one per HTTP call).
        latest_client = _make_async_client(latest_resp)
        list_client = _make_async_client(list_resp)

        with patch("app.routers.app_release.httpx.AsyncClient", side_effect=[latest_client, list_client]):
            response = authenticated_client.get("/api/app/latest")

        assert response.status_code == 404

    def test_returns_502_when_github_api_unreachable(self, authenticated_client):
        with patch("app.routers.app_release.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            response = authenticated_client.get("/api/app/latest")

        assert response.status_code == 502

    def test_returns_502_when_github_api_returns_server_error(self, authenticated_client):
        mock_resp = _make_github_response(500)
        with patch("app.routers.app_release.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            response = authenticated_client.get("/api/app/latest")

        assert response.status_code == 502


class TestAppLatestNegativeCache:
    """Tests for negative-result caching: errors should not cause a fresh GitHub call each time."""

    def test_502_error_is_cached_and_github_not_called_again(self, authenticated_client):
        """A 502 from GitHub should be cached; the second request must not call GitHub again."""
        with patch("app.routers.app_release.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            response1 = authenticated_client.get("/api/app/latest")
            response2 = authenticated_client.get("/api/app/latest")

        assert response1.status_code == 502
        assert response2.status_code == 502
        # GitHub should only be called once; second response served from error cache
        assert mock_client.get.call_count == 1

    def test_404_error_is_cached_and_github_not_called_again(self, authenticated_client):
        """A 404 from GitHub should also be cached for the negative TTL."""
        mock_resp = _make_github_response(404)
        with patch("app.routers.app_release.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value = mock_client

            response1 = authenticated_client.get("/api/app/latest")
            response2 = authenticated_client.get("/api/app/latest")

        assert response1.status_code == 404
        assert response2.status_code == 404
        assert mock_client.get.call_count == 1

    def test_error_cache_expires_and_github_is_retried(self, authenticated_client):
        """Once the negative TTL has elapsed, a fresh GitHub call should be made."""
        import time as time_module

        mock_resp = _make_github_response(500)
        with patch("app.routers.app_release.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            authenticated_client.get("/api/app/latest")

            # Simulate the error cache having expired
            app_release_module._APP_LATEST_ERROR_CACHE["fetched_at"] = (
                time_module.monotonic() - app_release_module._APP_LATEST_ERROR_CACHE_TTL - 1
            )

            authenticated_client.get("/api/app/latest")

        # GitHub should be called twice: once on first request, once after expiry
        assert mock_client.get.call_count == 2

    def test_successful_result_clears_error_cache(self, authenticated_client):
        """After a successful response, the error cache should not interfere."""
        # Pre-populate the error cache with a stale error
        import time as time_module
        app_release_module._APP_LATEST_ERROR_CACHE["error"] = {"status_code": 502, "detail": "old error"}
        app_release_module._APP_LATEST_ERROR_CACHE["fetched_at"] = (
            time_module.monotonic() - app_release_module._APP_LATEST_ERROR_CACHE_TTL - 1
        )

        mock_resp = _make_github_response(200, SAMPLE_RELEASE)
        with patch("app.routers.app_release.httpx.AsyncClient", return_value=_make_async_client(mock_resp)):
            response = authenticated_client.get("/api/app/latest")

        assert response.status_code == 200
        assert response.json()["version"] == "1.2.3"


PREVIOUS_RELEASE = {
    "tag_name": "v1.1.0",
    "published_at": "2026-03-01T10:00:00Z",
    "assets": [
        {
            "name": "app-release.apk",
            "browser_download_url": "https://github.com/lucas42/lucos_photos_android/releases/download/v1.1.0/app-release.apk",
        }
    ],
}

RELEASE_WITHOUT_APK = {
    "tag_name": "v1.2.0",
    "published_at": "2026-03-09T12:00:00Z",
    "assets": [],
}


class TestAppLatestFallback:
    """Tests for the fallback behaviour when the latest release has no APK yet."""

    def test_falls_back_to_previous_release_when_latest_has_no_apk(self, authenticated_client):
        """When the latest release has no APK, the previous release is served with updating=True."""
        latest_resp = _make_github_response(200, RELEASE_WITHOUT_APK)
        list_resp = _make_github_response(200, [RELEASE_WITHOUT_APK, PREVIOUS_RELEASE])

        with patch("app.routers.app_release.httpx.AsyncClient", side_effect=[
            _make_async_client(latest_resp),
            _make_async_client(list_resp),
        ]):
            response = authenticated_client.get("/api/app/latest")

        assert response.status_code == 200
        data = response.json()
        assert data["version"] == "1.1.0"
        assert data["download_url"] == "https://github.com/lucas42/lucos_photos_android/releases/download/v1.1.0/app-release.apk"
        assert data["updating"] is True

    def test_updating_flag_absent_on_normal_release(self, authenticated_client):
        """When the latest release has an APK, updating is not included in the response."""
        mock_resp = _make_github_response(200, SAMPLE_RELEASE)
        with patch("app.routers.app_release.httpx.AsyncClient", return_value=_make_async_client(mock_resp)):
            response = authenticated_client.get("/api/app/latest")

        assert response.status_code == 200
        data = response.json()
        assert "updating" not in data

    def test_returns_502_when_list_api_unreachable_after_no_apk(self, authenticated_client):
        """502 is returned if the fallback list API call fails."""
        latest_resp = _make_github_response(200, RELEASE_WITHOUT_APK)
        latest_client = _make_async_client(latest_resp)

        error_client = AsyncMock()
        error_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        error_client.__aenter__ = AsyncMock(return_value=error_client)
        error_client.__aexit__ = AsyncMock(return_value=None)

        with patch("app.routers.app_release.httpx.AsyncClient", side_effect=[latest_client, error_client]):
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
