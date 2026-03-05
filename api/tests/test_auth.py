"""Tests for the lucos_authentication session auth middleware (verify_session).

These tests focus on the behaviour of verify_session itself: cookie handling,
auth service validation, browser redirect vs. API 401, etc.

The M2M (CLIENT_KEYS) auth on the upload endpoint is covered in test_main.py.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx


AUTH_DOMAIN = "https://auth.l42.eu"


class TestVerifySessionNoCookie:
    """No auth_token cookie supplied."""

    def test_api_request_returns_401(self, client):
        """JSON / API requests without a cookie get a 401."""
        response = client.get("/photos", headers={"Accept": "application/json"})
        assert response.status_code == 401

    def test_browser_request_redirects_to_auth(self, client):
        """Browser requests (Accept: text/html) without a cookie are redirected."""
        response = client.get(
            "/photos",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        location = response.headers["location"]
        assert location.startswith(f"{AUTH_DOMAIN}/authenticate?redirect_uri=")

    def test_browser_redirect_includes_current_url(self, client):
        """The redirect_uri in the redirect must include the originally requested path."""
        from urllib.parse import urlparse, parse_qs, unquote
        response = client.get(
            "/photos",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        location = response.headers["location"]
        assert "redirect_uri=" in location
        parsed = urlparse(location)
        redirect_uri = unquote(parse_qs(parsed.query)["redirect_uri"][0])
        assert "/photos" in redirect_uri

    def test_browser_redirect_url_encodes_query_params(self, client):
        """Query parameters in the original URL must be URL-encoded in redirect_uri.

        Without encoding, ?limit=10&offset=50 would produce:
          ...?redirect_uri=http://example.com/photos?limit=10&offset=50
        where &offset=50 becomes a separate auth-service query param, not part of
        redirect_uri — so the user would be returned to /photos?limit=10 only.
        """
        response = client.get(
            "/photos?limit=10&offset=50",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        location = response.headers["location"]
        # The redirect_uri value must be URL-encoded — & should appear as %26
        assert "%26" in location
        # The auth domain's own query string must only contain redirect_uri
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(location)
        qs = parse_qs(parsed.query)
        assert list(qs.keys()) == ["redirect_uri"], (
            f"Expected only 'redirect_uri' in auth URL query string, got: {list(qs.keys())}"
        )


class TestVerifySessionInvalidToken:
    """auth_token cookie present but invalid (auth service rejects it)."""

    def _mock_auth_response(self, status_code=401, body=None):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Unauthorized",
            request=MagicMock(),
            response=MagicMock(status_code=status_code),
        )
        return mock_resp

    def test_invalid_token_returns_401_for_api_requests(self, client):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Unauthorized", request=MagicMock(), response=MagicMock(status_code=401)
        )
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("app.main.httpx.AsyncClient", return_value=mock_client):
            response = client.get(
                "/photos",
                headers={"Accept": "application/json"},
                cookies={"auth_token": "bad-token"},
            )
        assert response.status_code == 401

    def test_auth_service_returns_no_id_causes_401(self, client):
        """Auth service returns 200 but with no id — treat as unauthenticated."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"id": None}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("app.main.httpx.AsyncClient", return_value=mock_client):
            response = client.get(
                "/photos",
                headers={"Accept": "application/json"},
                cookies={"auth_token": "empty-id-token"},
            )
        assert response.status_code == 401

    def test_auth_service_network_error_causes_401(self, client):
        """Network failure contacting the auth service — fail closed."""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("unreachable"))

        with patch("app.main.httpx.AsyncClient", return_value=mock_client):
            response = client.get(
                "/photos",
                headers={"Accept": "application/json"},
                cookies={"auth_token": "any-token"},
            )
        assert response.status_code == 401


class TestVerifySessionValidToken:
    """auth_token cookie present and valid — requests should proceed."""

    def _mock_valid_auth_client(self, user_id=1):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"id": user_id}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        return mock_client

    def test_valid_token_allows_access(self, client):
        mock_client = self._mock_valid_auth_client()
        with patch("app.main.httpx.AsyncClient", return_value=mock_client):
            response = client.get(
                "/photos",
                headers={"Accept": "application/json"},
                cookies={"auth_token": "valid-token"},
            )
        assert response.status_code == 200

    def test_auth_service_called_with_token(self, client):
        """The auth service must be called with the cookie value as the token param."""
        mock_client = self._mock_valid_auth_client()
        with patch("app.main.httpx.AsyncClient", return_value=mock_client):
            client.get(
                "/photos",
                headers={"Accept": "application/json"},
                cookies={"auth_token": "my-specific-token"},
            )
        mock_client.get.assert_called_once()
        call_kwargs = mock_client.get.call_args
        assert call_kwargs[1]["params"]["token"] == "my-specific-token"

    def test_auth_service_url_is_hardcoded(self, client):
        """The auth service URL must always be auth.l42.eu — never configurable."""
        mock_client = self._mock_valid_auth_client()
        with patch("app.main.httpx.AsyncClient", return_value=mock_client):
            client.get(
                "/photos",
                headers={"Accept": "application/json"},
                cookies={"auth_token": "valid-token"},
            )
        call_args = mock_client.get.call_args
        called_url = call_args[0][0] if call_args[0] else call_args[1].get("url", "")
        assert "auth.l42.eu" in called_url


class TestUploadStillUsesMachineAuth:
    """POST /photos must continue to use CLIENT_KEYS (M2M) auth, not session auth."""

    def test_upload_with_key_auth_still_works(self, client, tmp_path):
        """Uploading with a valid key header should not require a session cookie."""
        import app.main as main_module
        VALID_IMAGE_CONTENT = bytes.fromhex(
            "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffdb0043010909090c0b0c180d0d1832211c213232323232323232323232323232323232323232323232323232323232323232323232323232323232323232323232323232ffc00011080001000103012200021101031101ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffc4001f0100030101010101010101010000000000000102030405060708090a0bffc400b51100020102040403040705040400010277000102031104052131061241510761711322328108144291a1b1c109233352f0156272d10a162434e125f11718191a262728292a35363738393a434445464748494a535455565758595a636465666768696a737475767778797a82838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9faffda000c03010002110311003f00e2e8a28af993f713ffd9"
        )
        response = client.post(
            "/photos",
            files={"file": ("photo.jpg", VALID_IMAGE_CONTENT, "image/jpeg")},
            headers={"Authorization": "key validkey"},
        )
        assert response.status_code == 201

    def test_upload_without_key_returns_401(self, client):
        """Uploading without any auth header should still return 401 (M2M scheme)."""
        response = client.post(
            "/photos",
            files={"file": ("photo.jpg", b"data", "image/jpeg")},
        )
        assert response.status_code == 401

    def test_upload_with_session_cookie_but_no_key_returns_401(self, client):
        """A valid session cookie must NOT satisfy the M2M upload auth requirement."""
        response = client.post(
            "/photos",
            files={"file": ("photo.jpg", b"data", "image/jpeg")},
            cookies={"auth_token": "valid-session-token"},
        )
        assert response.status_code == 401
