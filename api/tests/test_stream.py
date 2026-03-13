"""Tests for the /stream WebSocket endpoint."""
import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app, _ws_clients


class TestWebSocketStream:
    def test_rejects_connection_without_cookie(self, client):
        with pytest.raises(Exception):
            with client.websocket_connect("/stream"):
                pass

    def test_rejects_connection_with_invalid_token(self, client):
        client.cookies.set("auth_token", "badtoken")
        try:
            with patch.object(
                main_module,
                "_validate_token_with_auth_service",
                new=AsyncMock(return_value=None),
            ):
                with pytest.raises(Exception):
                    with client.websocket_connect("/stream"):
                        pass
        finally:
            client.cookies.clear()

    def test_accepts_connection_with_valid_token(self, client):
        client.cookies.set("auth_token", "validtoken")
        try:
            with patch.object(
                main_module,
                "_validate_token_with_auth_service",
                new=AsyncMock(return_value={"id": "user1"}),
            ):
                with client.websocket_connect("/stream") as ws:
                    msg = json.loads(ws.receive_text())
                    assert msg == {"type": "connected"}
        finally:
            client.cookies.clear()

    def test_broadcast_sends_to_connected_clients(self, client):
        client.cookies.set("auth_token", "validtoken")
        try:
            with patch.object(
                main_module,
                "_validate_token_with_auth_service",
                new=AsyncMock(return_value={"id": "user1"}),
            ):
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
        client.cookies.set("auth_token", "validtoken")
        try:
            with patch.object(
                main_module,
                "_validate_token_with_auth_service",
                new=AsyncMock(return_value={"id": "user1"}),
            ):
                with client.websocket_connect("/stream") as ws:
                    ws.receive_text()  # "connected"
                    # While connected, the client should be registered
                    assert len(_ws_clients) >= 1

            # After disconnect, _ws_clients should be empty
            assert len(_ws_clients) == 0
        finally:
            client.cookies.clear()
