import pytest
import app.main
from pathlib import Path


def test_root_requires_auth(client):
    """Unauthenticated requests to GET / must not return the page."""
    response = client.get("/", headers={"Accept": "application/json"})
    assert response.status_code == 401


def test_root_redirects_browser_to_auth(client):
    """Browser requests to GET / without a session must be redirected to the auth service."""
    response = client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 302
    assert "auth.l42.eu" in response.headers["location"]


def test_root_returns_html(authenticated_client):
    response = authenticated_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "lucos-navbar" in response.text
    assert 'src="/lucos_navbar.js"' in response.text


def test_root_includes_nav(authenticated_client):
    """The nav must be server-rendered with the correct links."""
    response = authenticated_client.get("/")
    assert response.status_code == 200
    assert 'href="/"' in response.text
    assert 'href="/people"' in response.text
    assert 'href="/app"' in response.text


def test_root_marks_photos_as_current(authenticated_client):
    """The Photos nav link should be marked as current-page on the index."""
    response = authenticated_client.get("/")
    assert response.status_code == 200
    # current-page class should appear on or near the Photos link
    assert "current-page" in response.text


# ---------------------------------------------------------------------------
# Pagination — HTML structure tests
# ---------------------------------------------------------------------------

class TestPaginationHtml:
    def test_index_html_includes_pagination_nav(self, authenticated_client):
        """The index page must include a pagination nav element."""
        response = authenticated_client.get("/")
        assert response.status_code == 200
        assert 'id="pagination-bar"' in response.text

    def test_index_html_pagination_has_aria_label(self, authenticated_client):
        """The pagination nav must be accessible with an aria-label."""
        response = authenticated_client.get("/")
        assert response.status_code == 200
        assert 'aria-label="Page navigation"' in response.text

    def test_index_html_references_page_size(self, authenticated_client):
        """The JS must define a PAGE_SIZE constant used to control how many photos per page."""
        response = authenticated_client.get("/")
        assert response.status_code == 200
        assert 'PAGE_SIZE' in response.text

    def test_index_html_reads_page_query_param(self, authenticated_client):
        """The JS must read the 'page' query parameter to support URL-based pagination."""
        response = authenticated_client.get("/")
        assert response.status_code == 200
        assert "getCurrentPage" in response.text
        assert "'page'" in response.text

    def test_index_html_renders_pagination_links(self, authenticated_client):
        """The JS must include renderPagination logic for previous/next links."""
        response = authenticated_client.get("/")
        assert response.status_code == 200
        assert "renderPagination" in response.text
        assert "Previous" in response.text
        assert "Next" in response.text


# ---------------------------------------------------------------------------
# Photo detail page HTML tests
# ---------------------------------------------------------------------------

class TestPhotoDetailHtml:
    def test_photo_page_requires_auth(self, client, db_session):
        """Unauthenticated requests to GET /photos/{id} (HTML) must redirect to auth."""
        from lucos_photos_common.models import Photo, ProcessingStatus, ProcessingState
        photo = Photo(sha256_hash="a" * 64, file_extension="jpg")
        db_session.add(photo)
        db_session.flush()
        db_session.add(ProcessingStatus(photo_id=photo.id, state=ProcessingState.complete))
        db_session.commit()

        response = client.get(f"/photos/{photo.id}", headers={"Accept": "text/html"}, follow_redirects=False)
        assert response.status_code == 302
        assert "auth.l42.eu" in response.headers["location"]

    def test_photo_page_returns_html_with_data(self, authenticated_client, db_session):
        """GET /photos/{id} with Accept: text/html returns HTML with photo data baked in."""
        from lucos_photos_common.models import Photo, ProcessingStatus, ProcessingState
        photo = Photo(sha256_hash="b" * 64, file_extension="jpg", width=1920, height=1080)
        db_session.add(photo)
        db_session.flush()
        db_session.add(ProcessingStatus(photo_id=photo.id, state=ProcessingState.complete))
        db_session.commit()

        response = authenticated_client.get(f"/photos/{photo.id}", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        # Photo data baked in server-side
        assert str(photo.id) in response.text
        assert "lucos-navbar" in response.text

    def test_photo_page_shows_dimensions(self, authenticated_client, db_session):
        """Photo dimensions should be server-rendered into the HTML."""
        from lucos_photos_common.models import Photo, ProcessingStatus, ProcessingState
        photo = Photo(sha256_hash="c" * 64, file_extension="jpg", width=800, height=600)
        db_session.add(photo)
        db_session.flush()
        db_session.add(ProcessingStatus(photo_id=photo.id, state=ProcessingState.complete))
        db_session.commit()

        response = authenticated_client.get(f"/photos/{photo.id}", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert "800" in response.text
        assert "600" in response.text

    def test_photo_page_shows_extension(self, authenticated_client, db_session):
        """File extension should be rendered in the HTML."""
        from lucos_photos_common.models import Photo, ProcessingStatus, ProcessingState
        photo = Photo(sha256_hash="d" * 64, file_extension="png", width=100, height=100)
        db_session.add(photo)
        db_session.flush()
        db_session.add(ProcessingStatus(photo_id=photo.id, state=ProcessingState.complete))
        db_session.commit()

        response = authenticated_client.get(f"/photos/{photo.id}", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert "PNG" in response.text

    def test_photo_page_includes_nav(self, authenticated_client, db_session):
        """The photo page must include the shared site nav."""
        from lucos_photos_common.models import Photo, ProcessingStatus, ProcessingState
        photo = Photo(sha256_hash="e" * 64, file_extension="jpg")
        db_session.add(photo)
        db_session.flush()
        db_session.add(ProcessingStatus(photo_id=photo.id, state=ProcessingState.complete))
        db_session.commit()

        response = authenticated_client.get(f"/photos/{photo.id}", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert 'href="/"' in response.text
        assert 'href="/people"' in response.text

    def test_photo_page_json_still_works(self, authenticated_client, db_session):
        """GET /photos/{id} with Accept: application/json still returns JSON."""
        from lucos_photos_common.models import Photo, ProcessingStatus, ProcessingState
        photo = Photo(sha256_hash="f" * 64, file_extension="jpg")
        db_session.add(photo)
        db_session.flush()
        db_session.add(ProcessingStatus(photo_id=photo.id, state=ProcessingState.complete))
        db_session.commit()

        response = authenticated_client.get(f"/photos/{photo.id}", headers={"Accept": "application/json"})
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(photo.id)

    def test_photo_page_html_has_vary_accept(self, authenticated_client, db_session):
        """HTML response must include Vary: Accept so caches don't serve it as JSON."""
        from lucos_photos_common.models import Photo, ProcessingStatus, ProcessingState
        photo = Photo(sha256_hash="g" * 64, file_extension="jpg")
        db_session.add(photo)
        db_session.flush()
        db_session.add(ProcessingStatus(photo_id=photo.id, state=ProcessingState.complete))
        db_session.commit()

        response = authenticated_client.get(f"/photos/{photo.id}", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert response.headers.get("vary") == "Accept"

    def test_photo_page_json_has_vary_accept(self, authenticated_client, db_session):
        """JSON response must include Vary: Accept so caches don't serve it as HTML."""
        from lucos_photos_common.models import Photo, ProcessingStatus, ProcessingState
        photo = Photo(sha256_hash="h" * 64, file_extension="jpg")
        db_session.add(photo)
        db_session.flush()
        db_session.add(ProcessingStatus(photo_id=photo.id, state=ProcessingState.complete))
        db_session.commit()

        response = authenticated_client.get(f"/photos/{photo.id}", headers={"Accept": "application/json"})
        assert response.status_code == 200
        assert response.headers.get("vary") == "Accept"


# ---------------------------------------------------------------------------
# People page HTML tests
# ---------------------------------------------------------------------------

class TestPeoplePageHtml:
    def test_people_page_requires_auth(self, client):
        """Unauthenticated requests to GET /people (HTML) must redirect to auth."""
        response = client.get("/people", headers={"Accept": "text/html"}, follow_redirects=False)
        assert response.status_code == 302
        assert "auth.l42.eu" in response.headers["location"]

    def test_people_page_returns_html(self, authenticated_client, db_session):
        """GET /people with Accept: text/html returns the people page with the search component."""
        from lucos_photos_common.models import Person
        person = Person(display_name="Test Person")
        db_session.add(person)
        db_session.commit()

        response = authenticated_client.get("/people", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "lucos-navbar" in response.text
        # lucos-search is rendered per person card (only appears when people exist)
        assert "lucos-search" in response.text

    def test_people_page_renders_people_server_side(self, authenticated_client, db_session):
        """Person names must be rendered in the HTML, not left to client-side JS."""
        from lucos_photos_common.models import Person
        person = Person(display_name="Alice Example")
        db_session.add(person)
        db_session.commit()

        response = authenticated_client.get("/people", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert "Alice Example" in response.text

    def test_people_page_shows_empty_state(self, authenticated_client):
        """When there are no people, the empty state message is server-rendered."""
        response = authenticated_client.get("/people", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert "No people detected yet" in response.text

    def test_people_page_injects_arachne_key(self, authenticated_client, db_session, monkeypatch):
        """The arachne key from KEY_LUCOS_ARACHNE env var is injected into person cards."""
        from lucos_photos_common.models import Person
        person = Person(display_name="Test Person")
        db_session.add(person)
        db_session.commit()

        monkeypatch.setenv("KEY_LUCOS_ARACHNE", "test-arachne-key-abc123")
        response = authenticated_client.get("/people", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert "test-arachne-key-abc123" in response.text
        assert "__ARACHNE_KEY__" not in response.text

    def test_people_page_handles_missing_arachne_key(self, authenticated_client, monkeypatch):
        """If KEY_LUCOS_ARACHNE is not set, no placeholder leaks into the HTML."""
        monkeypatch.delenv("KEY_LUCOS_ARACHNE", raising=False)
        response = authenticated_client.get("/people", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert "__ARACHNE_KEY__" not in response.text

    def test_people_page_marks_people_as_current(self, authenticated_client):
        """The People nav link should be marked as current-page on the people page."""
        response = authenticated_client.get("/people", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert "current-page" in response.text

    def test_people_returns_json_by_default(self, authenticated_client):
        """GET /people with Accept: application/json returns a JSON object with a people key."""
        response = authenticated_client.get("/people", headers={"Accept": "application/json"})
        assert response.status_code == 200
        body = response.json()
        assert body["people"] == []
        assert body["total"] == 0

    def test_people_html_has_vary_accept(self, authenticated_client):
        """HTML response must include Vary: Accept so caches don't serve it as JSON."""
        response = authenticated_client.get("/people", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert response.headers.get("vary") == "Accept"

    def test_people_json_has_vary_accept(self, authenticated_client):
        """JSON response must include Vary: Accept so caches don't serve it as HTML."""
        response = authenticated_client.get("/people", headers={"Accept": "application/json"})
        assert response.status_code == 200
        assert response.headers.get("vary") == "Accept"
