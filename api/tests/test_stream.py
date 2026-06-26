"""Tests for the /stream WebSocket endpoint.

The websocket authenticates via the ``aithne_session`` cookie (not ``auth_token``).
We inject a mock JWKS client so tests exercise the real jwt.decode path without
a live aithne endpoint.
"""
import asyncio
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

import app.auth as auth_module
import app.main as main_module
from app.main import app, _ws_clients


# ---------------------------------------------------------------------------
# Test key setup (mirrors test_auth.py)
# ---------------------------------------------------------------------------

_PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
_PUBLIC_KEY = _PRIVATE_KEY.public_key()
_TEST_KID = "test-key-id"
AITHNE_ORIGIN = "http://aithne.test"


class _MockSigningKey:
    def __init__(self):
        self.key = _PUBLIC_KEY
        self.key_id = _TEST_KID


class _MockJWKSClient:
    def __init__(self, fail=False):
        self._fail = fail
        self.jwk_set_data = {"keys": []}

    def get_signing_key_from_jwt(self, token):
        if self._fail:
            raise jwt.exceptions.PyJWKClientConnectionError("down")
        return _MockSigningKey()

    def get_jwk_set(self, refresh=False):
        from unittest.mock import MagicMock
        return MagicMock()


def _make_token(scopes=None, principal_class="human", expired=False):
    now = int(time.time())
    # Use 60s past to safely exceed the 30s CLOCK_SKEW leeway in auth.py.
    exp = (now - 60) if expired else (now + 900)
    payload = {
        "iss": AITHNE_ORIGIN,
        "sub": "test-user",
        "aud": ["l42.eu"],
        "iat": now,
        "exp": exp,
        "jti": "test-jti",
        "principal_class": principal_class,
        "scopes": scopes or [],
    }
    return jwt.encode(payload, _PRIVATE_KEY, algorithm="ES256", headers={"kid": _TEST_KID})


@pytest.fixture(autouse=True)
def inject_test_jwks(monkeypatch):
    # AITHNE_ORIGIN is a module-level constant in auth.py; patch both env and
    # the derived module attributes so jwt.decode iss-check uses the test value.
    monkeypatch.setenv("AITHNE_ORIGIN", AITHNE_ORIGIN)
    monkeypatch.setattr(auth_module, "AITHNE_ORIGIN", AITHNE_ORIGIN)
    monkeypatch.setattr(auth_module, "AITHNE_LOGIN_URL", f"{AITHNE_ORIGIN}/auth/login")
    auth_module._set_jwks_client(_MockJWKSClient())
    yield
    auth_module._set_jwks_client(None)


# ---------------------------------------------------------------------------
# WebSocket auth tests
# ---------------------------------------------------------------------------

class TestWebSocketStream:
    def test_rejects_connection_from_cross_origin(self, client):
        """Cross-origin Origin header → connection rejected even with a valid token.

        aithne_session is SameSite=None, so a browser page at evil.example.com
        can open a WebSocket with the user's cookie attached.  The Origin check
        must block this before the token is even inspected.
        """
        token = _make_token(scopes=["photos:use"])
        client.cookies.set("aithne_session", token)
        try:
            with pytest.raises(Exception):
                with client.websocket_connect(
                    "/stream", headers={"origin": "https://evil.example.com"}
                ):
                    pass
        finally:
            client.cookies.clear()

    def test_rejects_connection_without_cookie(self, client):
        """No aithne_session cookie → close with 4401."""
        with pytest.raises(Exception):
            with client.websocket_connect("/stream"):
                pass

    def test_rejects_connection_with_invalid_token(self, client):
        """Invalid JWT → close with 4401."""
        auth_module._set_jwks_client(_MockJWKSClient(fail=True))
        client.cookies.set("aithne_session", "not.a.valid.jwt")
        try:
            with pytest.raises(Exception):
                with client.websocket_connect("/stream"):
                    pass
        finally:
            client.cookies.clear()

    def test_rejects_connection_with_expired_token(self, client):
        """Expired token → close with 4401."""
        token = _make_token(scopes=["photos:use"], expired=True)
        client.cookies.set("aithne_session", token)
        try:
            with pytest.raises(Exception):
                with client.websocket_connect("/stream"):
                    pass
        finally:
            client.cookies.clear()

    def test_rejects_connection_with_valid_token_but_no_scope(self, client):
        """Valid session but missing photos:use → close with 4403."""
        token = _make_token(scopes=[])
        client.cookies.set("aithne_session", token)
        try:
            with pytest.raises(Exception):
                with client.websocket_connect("/stream"):
                    pass
        finally:
            client.cookies.clear()

    def test_accepts_connection_with_valid_token_and_scope(self, client):
        """Valid session with photos:use → connection accepted."""
        token = _make_token(scopes=["photos:use"])
        client.cookies.set("aithne_session", token)
        try:
            with client.websocket_connect("/stream") as ws:
                msg = json.loads(ws.receive_text())
                assert msg == {"type": "connected"}
        finally:
            client.cookies.clear()

    def test_broadcast_sends_to_connected_clients(self, client):
        token = _make_token(scopes=["photos:use"])
        client.cookies.set("aithne_session", token)
        try:
            with client.websocket_connect("/stream") as ws:
                # Consume the "connected" message
                ws.receive_text()

                # Broadcast directly via the _broadcast coroutine
                asyncio.run(
                    main_module._broadcast(json.dumps({"type": "photoProcessed", "photoId": "abc-123"}))
                )

                msg = json.loads(ws.receive_text())
                assert msg == {"type": "photoProcessed", "photoId": "abc-123"}
        finally:
            client.cookies.clear()

    def test_client_removed_on_disconnect(self, client):
        token = _make_token(scopes=["photos:use"])
        client.cookies.set("aithne_session", token)
        try:
            with client.websocket_connect("/stream") as ws:
                ws.receive_text()  # "connected"
                assert len(_ws_clients) >= 1

            # After disconnect, _ws_clients should be empty
            assert len(_ws_clients) == 0
        finally:
            client.cookies.clear()
