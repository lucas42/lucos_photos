import pytest
from fastapi.testclient import TestClient
from pathlib import Path

def test_root_returns_index_html(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "lucos-navbar" in response.text
    assert 'src="/lucos_navbar.js"' in response.text

def test_icon_returns_image(client):
    response = client.get("/icon")
    assert response.status_code == 200
    assert "image/png" in response.headers["content-type"]
