import pytest
import app.main

@pytest.fixture
def mock_static_dir(tmp_path, monkeypatch):
    """
    Creates a temporary directory with dummy static files and 
    patches app.main.STATIC_DIR to use it.
    """
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><lucos-navbar>Photos</lucos-navbar><script src=\"/lucos_navbar.js\"></script></html>")
    (static_dir / "icon.png").write_text("fake-png-data")
    (static_dir / "lucos_navbar.js").write_text("console.log('navbar');")
    
    monkeypatch.setattr(app.main, "STATIC_DIR", static_dir)
    return static_dir

def test_root_requires_auth(client, mock_static_dir):
    """Unauthenticated requests to GET / must not return the page."""
    response = client.get("/", headers={"Accept": "application/json"})
    assert response.status_code == 401


def test_root_redirects_browser_to_auth(client, mock_static_dir):
    """Browser requests to GET / without a session must be redirected to the auth service."""
    response = client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 302
    assert "auth.l42.eu" in response.headers["location"]


def test_root_returns_index_html(authenticated_client, mock_static_dir):
    response = authenticated_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "lucos-navbar" in response.text
    assert 'src="/lucos_navbar.js"' in response.text

def test_navbar_js_returns_script(client, mock_static_dir):
    response = client.get("/lucos_navbar.js")
    assert response.status_code == 200
    # Starlette/FastAPI will detect the .js extension and set the content type
    assert "javascript" in response.headers["content-type"]

def test_icon_returns_image(client, mock_static_dir):
    response = client.get("/icon")
    assert response.status_code == 200
    assert "image/png" in response.headers["content-type"]
