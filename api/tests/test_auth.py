"""Tests for the aithne JWKS session auth middleware (verify_session_or_key).

Uses a real ES256 test key pair to exercise the actual jwt.decode verification
path (contract C4: "a real token or a deliberate test double that exercises the
real JWKS/verification interface").

Coverage:
- Three-branch middleware (valid+scope, valid+no-scope, invalid/absent)
- /stream WebSocket auth (tested in test_stream.py)
- CSRF check on write methods
- /_info reachability without auth
- M2M key auth unchanged
- render-ui dev bypass
"""
import time
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse as _urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

import app.auth as auth_module


# ---------------------------------------------------------------------------
# Test key pair — generated once per module, reused across tests
# ---------------------------------------------------------------------------

_PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
_PUBLIC_KEY = _PRIVATE_KEY.public_key()
_TEST_KID = "test-key-id"

AITHNE_ORIGIN = "http://aithne.test"
APP_ORIGIN_TEST = "https://photos.test"


def _make_token(scopes=None, principal_class="human", expired=False, wrong_iss=False, wrong_aud=False, kid=_TEST_KID):
    """Mint a test JWT signed with the test ES256 key pair."""
    now = int(time.time())
    # Use 60s past to safely exceed the 30s CLOCK_SKEW leeway in auth.py.
    exp = (now - 60) if expired else (now + 900)
    iss = "https://evil.example.com" if wrong_iss else AITHNE_ORIGIN
    aud = ["wrong.example.com"] if wrong_aud else ["l42.eu"]
    payload = {
        "iss": iss,
        "sub": "test-user",
        "aud": aud,
        "iat": now,
        "exp": exp,
        "jti": "test-jti",
        "principal_class": principal_class,
        "scopes": scopes or [],
    }
    return jwt.encode(payload, _PRIVATE_KEY, algorithm="ES256", headers={"kid": kid})


class _MockSigningKey:
    """Minimal stand-in for a PyJWK signing key object."""
    def __init__(self, key=_PUBLIC_KEY, key_id=_TEST_KID):
        self.key = key
        self.key_id = key_id


class _MockJWKSClient:
    """JWKS client test double that returns the test public key for any token."""
    def __init__(self, fail=False, unknown_kid=False):
        self._fail = fail
        self._unknown_kid = unknown_kid
        self.jwk_set_data = {"keys": []}  # simulates a cached key set

    def get_signing_key_from_jwt(self, token):
        if self._fail:
            raise jwt.exceptions.PyJWKClientConnectionError("JWKS unreachable")
        if self._unknown_kid:
            raise jwt.exceptions.PyJWKClientError("Unable to find signing key")
        return _MockSigningKey()

    def get_jwk_set(self, refresh=False):
        return MagicMock()


@pytest.fixture(autouse=True)
def inject_test_jwks_and_origin(monkeypatch):
    """Inject test JWKS client and AITHNE_ORIGIN before each test.

    AITHNE_ORIGIN is a module-level constant evaluated at import time, so
    setenv alone is not enough — we must also patch the module attributes that
    were derived from it (AITHNE_ORIGIN itself and AITHNE_LOGIN_URL).
    """
    monkeypatch.setenv("AITHNE_ORIGIN", AITHNE_ORIGIN)
    monkeypatch.setattr(auth_module, "AITHNE_ORIGIN", AITHNE_ORIGIN)
    monkeypatch.setattr(auth_module, "AITHNE_LOGIN_URL", f"{AITHNE_ORIGIN}/auth/login")
    monkeypatch.setattr(auth_module, "APP_ORIGIN", APP_ORIGIN_TEST)
    auth_module._set_jwks_client(_MockJWKSClient())
    yield
    auth_module._set_jwks_client(None)


# ---------------------------------------------------------------------------
# Branch 1 — valid token with required scope
# ---------------------------------------------------------------------------

class TestValidTokenWithScope:
    def test_photos_use_scope_grants_access(self, client):
        token = _make_token(scopes=["photos:use"])
        response = client.get(
            "/photos",
            headers={"Accept": "application/json"},
            cookies={"aithne_session": token},
        )
        assert response.status_code == 200

    def test_render_ui_scope_grants_access_in_development(self, client, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "development")
        token = _make_token(scopes=["render-ui"])
        response = client.get(
            "/photos",
            headers={"Accept": "application/json"},
            cookies={"aithne_session": token},
        )
        assert response.status_code == 200

    def test_render_ui_scope_denied_in_production(self, client, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        token = _make_token(scopes=["render-ui"])
        response = client.get(
            "/photos",
            headers={"Accept": "application/json"},
            cookies={"aithne_session": token},
        )
        assert response.status_code == 403

    def test_agent_principal_class_with_scope_allowed(self, client):
        token = _make_token(scopes=["photos:use"], principal_class="agent")
        response = client.get(
            "/photos",
            headers={"Accept": "application/json"},
            cookies={"aithne_session": token},
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Branch 2 — valid token but missing scope → 403 (not a redirect)
# ---------------------------------------------------------------------------

class TestValidTokenMissingScope:
    def test_no_scope_returns_403_for_json(self, client):
        token = _make_token(scopes=[])
        response = client.get(
            "/photos",
            headers={"Accept": "application/json"},
            cookies={"aithne_session": token},
        )
        assert response.status_code == 403

    def test_no_scope_returns_403_for_browser(self, client):
        token = _make_token(scopes=[])
        response = client.get(
            "/photos",
            headers={"Accept": "text/html"},
            cookies={"aithne_session": token},
            follow_redirects=False,
        )
        assert response.status_code == 403

    def test_wrong_scope_returns_403(self, client):
        token = _make_token(scopes=["arachne:read"])
        response = client.get(
            "/photos",
            headers={"Accept": "application/json"},
            cookies={"aithne_session": token},
        )
        assert response.status_code == 403

    def test_missing_scope_does_not_redirect_to_login(self, client):
        """Must NOT redirect to login — that would create an infinite loop."""
        token = _make_token(scopes=[])
        response = client.get(
            "/photos",
            headers={"Accept": "text/html"},
            cookies={"aithne_session": token},
            follow_redirects=False,
        )
        # A 302 here would be a redirect-loop bug
        assert response.status_code == 403

    def test_unknown_principal_class_treated_as_unauthenticated(self, client):
        """Unrecognised principal_class → verify_aithne_token returns None → redirect."""
        token = _make_token(scopes=["photos:use"], principal_class="unknown_class")
        response = client.get(
            "/photos",
            headers={"Accept": "application/json"},
            cookies={"aithne_session": token},
            follow_redirects=False,
        )
        assert response.status_code == 302


# ---------------------------------------------------------------------------
# Branch 3 — no token or invalid/expired token → redirect to aithne login
# ---------------------------------------------------------------------------

class TestNoOrInvalidToken:
    def test_no_cookie_redirects_to_aithne_login(self, client):
        response = client.get(
            "/photos",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.headers["location"].startswith(f"{AITHNE_ORIGIN}/auth/login")

    def test_redirect_includes_next_full_url(self, client):
        response = client.get(
            "/photos",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        location = response.headers["location"]
        # ?next= must be a full absolute URL back to photos, not a bare path
        next_val = parse_qs(_urlparse(location).query)["next"][0]
        assert next_val == f"{APP_ORIGIN_TEST}/photos"

    def test_json_request_without_cookie_redirects(self, client):
        """Both JSON and HTML unauthenticated requests redirect — aithne is the gate."""
        response = client.get(
            "/photos",
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )
        assert response.status_code == 302

    def test_expired_token_redirects_to_login(self, client):
        token = _make_token(scopes=["photos:use"], expired=True)
        response = client.get(
            "/photos",
            headers={"Accept": "text/html"},
            cookies={"aithne_session": token},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.headers["location"].startswith(f"{AITHNE_ORIGIN}/auth/login")

    def test_wrong_iss_redirects_to_login(self, client):
        token = _make_token(scopes=["photos:use"], wrong_iss=True)
        response = client.get(
            "/photos",
            headers={"Accept": "application/json"},
            cookies={"aithne_session": token},
            follow_redirects=False,
        )
        assert response.status_code == 302

    def test_wrong_aud_redirects_to_login(self, client):
        token = _make_token(scopes=["photos:use"], wrong_aud=True)
        response = client.get(
            "/photos",
            headers={"Accept": "application/json"},
            cookies={"aithne_session": token},
            follow_redirects=False,
        )
        assert response.status_code == 302

    def test_redirect_uses_server_side_path_not_query_param(self, client):
        """?next= must be a full URL built from APP_ORIGIN server-side, never user input."""
        response = client.get("/photos", follow_redirects=False)
        location = response.headers["location"]
        next_val = parse_qs(_urlparse(location).query)["next"][0]
        # next= must be a full photos.test URL pointing to the actual path
        assert next_val == f"{APP_ORIGIN_TEST}/photos"
        # Must not contain any evil domain in any part of the redirect URL
        assert "evil" not in location

    def test_redirect_preserves_query_string(self, client):
        """Query string is included in the ?next= URL so the user returns to the right page."""
        response = client.get("/photos?page=2&sort=date", follow_redirects=False)
        assert response.status_code == 302
        location = response.headers["location"]
        next_val = parse_qs(_urlparse(location).query)["next"][0]
        assert next_val == f"{APP_ORIGIN_TEST}/photos?page=2&sort=date"

    def test_unknown_kid_redirects_to_login(self, client):
        auth_module._set_jwks_client(_MockJWKSClient(unknown_kid=True))
        token = _make_token(scopes=["photos:use"], kid="unknown-kid")
        response = client.get(
            "/photos",
            headers={"Accept": "application/json"},
            cookies={"aithne_session": token},
            follow_redirects=False,
        )
        assert response.status_code == 302


# ---------------------------------------------------------------------------
# /_info must stay reachable without auth (contract: exempt)
# ---------------------------------------------------------------------------

class TestInfoEndpointExempt:
    def test_info_accessible_without_auth(self, client):
        """/_info must not require auth — lucos_monitoring polls it unauthenticated."""
        response = client.get("/_info")
        assert response.status_code == 200

    def test_info_does_not_redirect_to_aithne(self, client):
        response = client.get("/_info", follow_redirects=False)
        assert response.status_code == 200
        assert not response.headers.get("location", "").startswith(AITHNE_ORIGIN)


# ---------------------------------------------------------------------------
# CSRF protection (C5) — cookie-authenticated write endpoints
# ---------------------------------------------------------------------------

class TestCsrfProtection:
    def _session_cookie(self):
        return _make_token(scopes=["photos:use"])

    def test_post_without_csrf_header_rejected_via_cookie_auth(self, client):
        """POST via cookie auth without X-Requested-With or valid Origin → 403."""
        response = client.post(
            "/people",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            content='{"displayName": "Test"}',
            cookies={"aithne_session": self._session_cookie()},
        )
        assert response.status_code == 403
        assert "CSRF" in response.json().get("detail", "")

    def test_post_with_xrw_header_passes_csrf_check(self, client):
        """X-Requested-With: XMLHttpRequest satisfies the CSRF check."""
        response = client.post(
            "/people",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            content='{"displayName": "Test"}',
            cookies={"aithne_session": self._session_cookie()},
        )
        # 403 with CSRF detail would be failure; endpoint may return 201/422
        assert not (response.status_code == 403 and "CSRF" in response.json().get("detail", ""))

    def test_post_with_l42_origin_passes_csrf_check(self, client):
        """Origin: https://photos.l42.eu satisfies the CSRF check."""
        response = client.post(
            "/people",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://photos.l42.eu",
            },
            content='{"displayName": "Test"}',
            cookies={"aithne_session": self._session_cookie()},
        )
        assert not (response.status_code == 403 and "CSRF" in response.json().get("detail", ""))

    def test_post_with_l42_eu_root_domain_passes_csrf_check(self, client):
        """Origin: https://l42.eu (root domain, not a subdomain) also passes."""
        response = client.post(
            "/people",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://l42.eu",
            },
            content='{"displayName": "Test"}',
            cookies={"aithne_session": self._session_cookie()},
        )
        assert not (response.status_code == 403 and "CSRF" in response.json().get("detail", ""))

    def test_post_with_evil_origin_rejected(self, client):
        """Origin from a non-l42.eu domain must not pass the CSRF check."""
        response = client.post(
            "/people",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://evil.example.com",
            },
            content='{"displayName": "Test"}',
            cookies={"aithne_session": self._session_cookie()},
        )
        assert response.status_code == 403
        assert "CSRF" in response.json().get("detail", "")

    def test_key_auth_post_bypasses_csrf_check(self, client):
        """M2M key auth on POST must NOT require CSRF headers."""
        response = client.post(
            "/people",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": "key validkey",
            },
            content='{"displayName": "Test"}',
        )
        # CSRF-403 would be wrong; endpoint may return 422/201 etc.
        assert not (response.status_code == 403 and "CSRF" in response.json().get("detail", ""))

    def test_get_request_does_not_require_csrf(self, client):
        """GET is not a state-mutating method — no CSRF check applies."""
        token = self._session_cookie()
        response = client.get(
            "/photos",
            headers={"Accept": "application/json"},
            cookies={"aithne_session": token},
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# M2M key auth — must remain unchanged after the migration
# ---------------------------------------------------------------------------

class TestMachineKeyAuth:
    def test_valid_bearer_key_grants_access(self, client):
        response = client.get(
            "/photos",
            headers={"Accept": "application/json", "Authorization": "Bearer validkey"},
        )
        assert response.status_code == 200

    def test_valid_key_scheme_grants_access(self, client):
        response = client.get(
            "/photos",
            headers={"Accept": "application/json", "Authorization": "key validkey"},
        )
        assert response.status_code == 200

    def test_invalid_key_falls_through_to_session(self, client):
        """Invalid key falls through to session check → redirect to login."""
        response = client.get(
            "/photos",
            headers={"Accept": "application/json", "Authorization": "Bearer badkey"},
            follow_redirects=False,
        )
        assert response.status_code == 302

    def test_upload_with_valid_key_succeeds(self, client):
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
        response = client.post(
            "/photos",
            files={"file": ("photo.jpg", b"data", "image/jpeg")},
        )
        assert response.status_code == 401

    def test_session_cookie_cannot_satisfy_key_only_upload_endpoint(self, client):
        token = _make_token(scopes=["photos:use"])
        response = client.post(
            "/photos",
            files={"file": ("photo.jpg", b"data", "image/jpeg")},
            cookies={"aithne_session": token},
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# safe_path — open-redirect prevention unit tests
# ---------------------------------------------------------------------------

class TestSafePath:
    def test_relative_path_is_allowed(self):
        from app.main import safe_path
        assert safe_path("/photos") == "/photos"

    def test_relative_path_with_query_is_allowed(self):
        from app.main import safe_path
        assert safe_path("/photos?limit=10") == "/photos?limit=10"

    def test_absolute_url_with_scheme_is_rejected(self):
        from app.main import safe_path
        assert safe_path("https://evil.example.com/steal") == "/"

    def test_protocol_relative_url_is_rejected(self):
        from app.main import safe_path
        assert safe_path("//evil.example.com/steal") == "/"

    def test_custom_fallback_is_used_on_rejection(self):
        from app.main import safe_path
        assert safe_path("https://evil.example.com", fallback="/safe") == "/safe"

    def test_empty_string_is_allowed(self):
        from app.main import safe_path
        assert safe_path("") == ""
