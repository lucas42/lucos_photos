import pytest
import app.main
from pathlib import Path


# Use the real static directory so tests reflect the actual shipped HTML.
REAL_STATIC_DIR = Path(app.main.__file__).parent / "static"


@pytest.fixture
def mock_static_dir(tmp_path, monkeypatch):
    """
    Creates a temporary directory with dummy static files and
    patches app.main.STATIC_DIR to use it.
    """
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><lucos-navbar>Photos</lucos-navbar><script src=\"/lucos_navbar.js\"></script></html>")

    monkeypatch.setattr(app.main, "STATIC_DIR", static_dir)
    return static_dir


@pytest.fixture
def real_static_dir(monkeypatch):
    """Patches STATIC_DIR to the real shipped static directory."""
    monkeypatch.setattr(app.main, "STATIC_DIR", REAL_STATIC_DIR)
    return REAL_STATIC_DIR


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


# ---------------------------------------------------------------------------
# Pagination — HTML structure tests
# ---------------------------------------------------------------------------

class TestPaginationHtml:
    def test_index_html_includes_pagination_nav(self, authenticated_client, real_static_dir):
        """The shipped index.html must include a pagination nav element."""
        response = authenticated_client.get("/")
        assert response.status_code == 200
        assert 'id="pagination"' in response.text

    def test_index_html_pagination_has_aria_label(self, authenticated_client, real_static_dir):
        """The pagination nav must be accessible with an aria-label."""
        response = authenticated_client.get("/")
        assert response.status_code == 200
        assert 'aria-label="Photo pagination"' in response.text

    def test_index_html_references_page_size(self, authenticated_client, real_static_dir):
        """The JS must define a PAGE_SIZE constant used to control how many photos per page."""
        response = authenticated_client.get("/")
        assert response.status_code == 200
        assert 'PAGE_SIZE' in response.text

    def test_index_html_reads_page_query_param(self, authenticated_client, real_static_dir):
        """The JS must read the 'page' query parameter to support URL-based pagination."""
        response = authenticated_client.get("/")
        assert response.status_code == 200
        assert "getCurrentPage" in response.text
        assert "'page'" in response.text

    def test_index_html_renders_pagination_links(self, authenticated_client, real_static_dir):
        """The JS must include renderPagination logic for previous/next links."""
        response = authenticated_client.get("/")
        assert response.status_code == 200
        assert "renderPagination" in response.text
        assert "Previous" in response.text
        assert "Next" in response.text


# ---------------------------------------------------------------------------
# People page HTML tests
# ---------------------------------------------------------------------------

class TestPeoplePageHtml:
    def test_people_page_requires_auth(self, client, real_static_dir):
        """Unauthenticated requests to GET /people (HTML) must redirect to auth."""
        response = client.get("/people", headers={"Accept": "text/html"}, follow_redirects=False)
        assert response.status_code == 302
        assert "auth.l42.eu" in response.headers["location"]

    def test_people_page_returns_html(self, authenticated_client, real_static_dir):
        """GET /people with Accept: text/html returns the people page."""
        response = authenticated_client.get("/people", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "lucos-navbar" in response.text
        assert "lucos-search" in response.text

    def test_people_page_injects_arachne_key(self, authenticated_client, real_static_dir, monkeypatch):
        """The arachne key from KEY_LUCOS_ARACHNE env var is injected into the HTML."""
        monkeypatch.setenv("KEY_LUCOS_ARACHNE", "test-arachne-key-abc123")
        response = authenticated_client.get("/people", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert "test-arachne-key-abc123" in response.text
        assert "__ARACHNE_KEY__" not in response.text

    def test_people_page_handles_missing_arachne_key(self, authenticated_client, real_static_dir, monkeypatch):
        """If KEY_LUCOS_ARACHNE is not set, the placeholder is replaced with an empty string."""
        monkeypatch.delenv("KEY_LUCOS_ARACHNE", raising=False)
        response = authenticated_client.get("/people", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert "__ARACHNE_KEY__" not in response.text

    def test_people_returns_json_by_default(self, authenticated_client, real_static_dir):
        """GET /people with Accept: application/json returns the JSON list."""
        response = authenticated_client.get("/people", headers={"Accept": "application/json"})
        assert response.status_code == 200
        assert response.json() == []
