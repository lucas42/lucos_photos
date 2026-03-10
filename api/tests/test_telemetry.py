"""Tests for POST /api/telemetry and GET /api/telemetry endpoints."""

AUTH_HEADER = {"Authorization": "Bearer validkey"}


class TestPostTelemetryAuth:
    """Authentication checks for POST /api/telemetry."""

    def test_no_auth_returns_401(self, client):
        response = client.post("/api/telemetry", json={"event_type": "sync_completed"})
        assert response.status_code == 401

    def test_wrong_key_returns_401(self, client):
        response = client.post(
            "/api/telemetry",
            json={"event_type": "sync_completed"},
            headers={"Authorization": "Bearer wrongkey"},
        )
        assert response.status_code == 401

    def test_valid_key_returns_201(self, client):
        response = client.post(
            "/api/telemetry",
            json={"event_type": "sync_completed"},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 201


class TestPostTelemetryValidation:
    """Input validation for POST /api/telemetry."""

    def test_missing_event_type_returns_422(self, client):
        response = client.post(
            "/api/telemetry",
            json={"app_version": "1.0"},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 422

    def test_invalid_timestamp_returns_422(self, client):
        response = client.post(
            "/api/telemetry",
            json={"event_type": "sync_completed", "timestamp": "not-a-date"},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 422


class TestPostTelemetrySuccess:
    """Happy-path tests for POST /api/telemetry."""

    def test_minimal_event_stored(self, client):
        response = client.post(
            "/api/telemetry",
            json={"event_type": "sync_completed"},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["event_type"] == "sync_completed"
        assert "id" in data
        assert "received_at" in data

    def test_full_event_stored(self, client):
        payload = {
            "event_type": "sync_completed",
            "app_version": "1.2.3",
            "timestamp": "2026-03-09T15:30:00Z",
            "data": {
                "duration_ms": 4200,
                "photos_synced": 15,
                "errors": 0,
            },
        }
        response = client.post("/api/telemetry", json=payload, headers=AUTH_HEADER)
        assert response.status_code == 201
        data = response.json()
        assert data["event_type"] == "sync_completed"
        assert data["app_version"] == "1.2.3"
        assert "2026-03-09" in data["timestamp"]
        assert data["data"]["duration_ms"] == 4200
        assert data["data"]["photos_synced"] == 15
        assert data["data"]["errors"] == 0

    def test_received_at_is_set_by_server(self, client):
        """received_at must always be set even when no client timestamp is provided."""
        response = client.post(
            "/api/telemetry",
            json={"event_type": "sync_completed"},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["received_at"] is not None
        assert data["timestamp"] is None

    def test_timestamp_with_z_suffix_accepted(self, client):
        response = client.post(
            "/api/telemetry",
            json={"event_type": "test_event", "timestamp": "2026-01-01T00:00:00Z"},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 201
        assert response.json()["timestamp"] is not None

    def test_event_type_without_data_is_accepted(self, client):
        response = client.post(
            "/api/telemetry",
            json={"event_type": "app_started", "app_version": "2.0.0"},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["event_type"] == "app_started"
        assert data["app_version"] == "2.0.0"
        assert data["data"] is None


class TestGetTelemetryAuth:
    """Authentication checks for GET /api/telemetry."""

    def test_no_auth_returns_401(self, client):
        response = client.get("/api/telemetry")
        assert response.status_code == 401

    def test_wrong_key_returns_401(self, client):
        response = client.get("/api/telemetry", headers={"Authorization": "Bearer wrongkey"})
        assert response.status_code == 401

    def test_valid_key_returns_200(self, client):
        response = client.get("/api/telemetry", headers=AUTH_HEADER)
        assert response.status_code == 200


class TestGetTelemetryList:
    """Query and listing behaviour for GET /api/telemetry."""

    def test_empty_list_initially(self, client):
        response = client.get("/api/telemetry", headers=AUTH_HEADER)
        assert response.status_code == 200
        data = response.json()
        assert data["events"] == []
        assert data["count"] == 0

    def test_returns_all_events(self, client):
        # Post two events
        client.post("/api/telemetry", json={"event_type": "sync_completed"}, headers=AUTH_HEADER)
        client.post("/api/telemetry", json={"event_type": "app_started"}, headers=AUTH_HEADER)

        response = client.get("/api/telemetry", headers=AUTH_HEADER)
        data = response.json()
        assert data["count"] == 2
        assert len(data["events"]) == 2

    def test_filter_by_event_type(self, client):
        client.post("/api/telemetry", json={"event_type": "sync_completed"}, headers=AUTH_HEADER)
        client.post("/api/telemetry", json={"event_type": "sync_completed"}, headers=AUTH_HEADER)
        client.post("/api/telemetry", json={"event_type": "app_started"}, headers=AUTH_HEADER)

        response = client.get("/api/telemetry?event_type=sync_completed", headers=AUTH_HEADER)
        data = response.json()
        assert data["count"] == 2
        for event in data["events"]:
            assert event["event_type"] == "sync_completed"

    def test_filter_by_event_type_returns_empty_for_unknown_type(self, client):
        client.post("/api/telemetry", json={"event_type": "sync_completed"}, headers=AUTH_HEADER)

        response = client.get("/api/telemetry?event_type=nonexistent_event", headers=AUTH_HEADER)
        data = response.json()
        assert data["count"] == 0
        assert data["events"] == []

    def test_since_filter_returns_valid_date(self, client):
        """since parameter should be accepted as a date string."""
        response = client.get("/api/telemetry?since=2020-01-01", headers=AUTH_HEADER)
        assert response.status_code == 200

    def test_invalid_since_returns_422(self, client):
        response = client.get("/api/telemetry?since=not-a-date", headers=AUTH_HEADER)
        assert response.status_code == 422

    def test_event_response_fields(self, client):
        """Each event in the list response must have the expected fields."""
        client.post(
            "/api/telemetry",
            json={
                "event_type": "sync_completed",
                "app_version": "1.0.0",
                "timestamp": "2026-03-09T12:00:00Z",
                "data": {"duration_ms": 1000},
            },
            headers=AUTH_HEADER,
        )

        response = client.get("/api/telemetry", headers=AUTH_HEADER)
        event = response.json()["events"][0]
        assert "id" in event
        assert event["event_type"] == "sync_completed"
        assert event["app_version"] == "1.0.0"
        assert event["timestamp"] is not None
        assert event["received_at"] is not None
        assert event["data"]["duration_ms"] == 1000
