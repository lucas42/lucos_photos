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


def test_root_marks_home_as_current(authenticated_client):
    """The Home nav link should be marked as current-page on the homepage."""
    response = authenticated_client.get("/")
    assert response.status_code == 200
    # current-page class should appear on the Home nav link
    assert "current-page" in response.text


# ---------------------------------------------------------------------------
# Pagination — HTML structure tests
# ---------------------------------------------------------------------------

class TestPaginationHtml:
    def test_photos_page_includes_pagination_nav(self, authenticated_client):
        """The /photos page must include a pagination nav element."""
        response = authenticated_client.get("/photos", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert 'id="pagination-bar"' in response.text

    def test_photos_page_pagination_has_aria_label(self, authenticated_client):
        """The pagination nav must be accessible with an aria-label."""
        response = authenticated_client.get("/photos", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert 'aria-label="Page navigation"' in response.text


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
# Photo detail: prev/next navigation
# ---------------------------------------------------------------------------

class TestPhotoNavigation:
    def _make_processed_photo(self, db_session, sha_char, taken_at=None):
        """Helper to create a processed photo with optional taken_at."""
        from lucos_photos_common.models import MediaItem, ProcessingStatus, ProcessingState
        from datetime import datetime, timezone
        photo = MediaItem(sha256_hash=sha_char * 64, file_extension="jpg", media_type="photo", taken_at=taken_at)
        db_session.add(photo)
        db_session.flush()
        db_session.add(ProcessingStatus(photo_id=photo.id, state=ProcessingState.complete))
        db_session.commit()
        return photo

    def test_single_photo_has_no_nav_links(self, authenticated_client, db_session):
        """A single photo should have null prev and next IDs."""
        photo = self._make_processed_photo(db_session, "a")
        response = authenticated_client.get(f"/photos/{photo.id}", headers={"Accept": "application/json"})
        assert response.status_code == 200
        data = response.json()
        assert data["prevPhotoId"] is None
        assert data["nextPhotoId"] is None

    def test_nav_links_with_taken_at_ordering(self, authenticated_client, db_session):
        """Photos ordered by taken_at DESC should have correct prev/next links."""
        from datetime import datetime, timezone
        older = self._make_processed_photo(db_session, "a", taken_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        middle = self._make_processed_photo(db_session, "b", taken_at=datetime(2024, 6, 1, tzinfo=timezone.utc))
        newer = self._make_processed_photo(db_session, "c", taken_at=datetime(2024, 12, 1, tzinfo=timezone.utc))

        # Middle photo should link to newer (prev) and older (next)
        response = authenticated_client.get(f"/photos/{middle.id}", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert str(newer.id) in response.text  # prev link
        assert str(older.id) in response.text  # next link

    def test_first_photo_has_no_prev(self, authenticated_client, db_session):
        """The newest photo should have no prev link."""
        from datetime import datetime, timezone
        older = self._make_processed_photo(db_session, "a", taken_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        newer = self._make_processed_photo(db_session, "b", taken_at=datetime(2024, 12, 1, tzinfo=timezone.utc))

        response = authenticated_client.get(f"/photos/{newer.id}", headers={"Accept": "text/html"})
        assert response.status_code == 200
        # Should have next (older) but prev should be disabled
        assert str(older.id) in response.text
        assert "photo-nav-disabled" in response.text

    def test_last_photo_has_no_next(self, authenticated_client, db_session):
        """The oldest photo should have no next link."""
        from datetime import datetime, timezone
        older = self._make_processed_photo(db_session, "a", taken_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        newer = self._make_processed_photo(db_session, "b", taken_at=datetime(2024, 12, 1, tzinfo=timezone.utc))

        response = authenticated_client.get(f"/photos/{older.id}", headers={"Accept": "text/html"})
        assert response.status_code == 200
        # Should have prev (newer) but next should be disabled
        assert str(newer.id) in response.text
        assert "photo-nav-disabled" in response.text

    def test_json_response_includes_nav_ids(self, authenticated_client, db_session):
        """JSON response should include prevPhotoId and nextPhotoId."""
        from datetime import datetime, timezone
        older = self._make_processed_photo(db_session, "a", taken_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        newer = self._make_processed_photo(db_session, "b", taken_at=datetime(2024, 12, 1, tzinfo=timezone.utc))

        response = authenticated_client.get(f"/photos/{older.id}", headers={"Accept": "application/json"})
        assert response.status_code == 200
        data = response.json()
        assert data["prevPhotoId"] == str(newer.id)
        assert data["nextPhotoId"] is None

    def test_unprocessed_photos_excluded_from_nav(self, authenticated_client, db_session):
        """Unprocessed photos should not appear in prev/next navigation."""
        from datetime import datetime, timezone
        from lucos_photos_common.models import MediaItem, ProcessingStatus, ProcessingState
        older = self._make_processed_photo(db_session, "a", taken_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        # Unprocessed photo in between
        pending = MediaItem(sha256_hash="b" * 64, file_extension="jpg", media_type="photo",
                           taken_at=datetime(2024, 6, 1, tzinfo=timezone.utc))
        db_session.add(pending)
        db_session.flush()
        db_session.add(ProcessingStatus(photo_id=pending.id, state=ProcessingState.pending))
        db_session.commit()
        newer = self._make_processed_photo(db_session, "c", taken_at=datetime(2024, 12, 1, tzinfo=timezone.utc))

        # Older's prev should be newer (skipping pending)
        response = authenticated_client.get(f"/photos/{older.id}", headers={"Accept": "application/json"})
        data = response.json()
        assert data["prevPhotoId"] == str(newer.id)
        assert data["nextPhotoId"] is None

    def test_nav_with_null_taken_at(self, authenticated_client, db_session):
        """Photos with NULL taken_at should sort after photos with taken_at (NULLS LAST)."""
        from datetime import datetime, timezone
        with_date = self._make_processed_photo(db_session, "a", taken_at=datetime(2024, 6, 1, tzinfo=timezone.utc))
        without_date = self._make_processed_photo(db_session, "b", taken_at=None)

        # Photo with taken_at should be "prev" (earlier in DESC order) relative to NULL
        response = authenticated_client.get(f"/photos/{without_date.id}", headers={"Accept": "application/json"})
        data = response.json()
        assert data["prevPhotoId"] == str(with_date.id)
        assert data["nextPhotoId"] is None


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
        """GET /people with Accept: text/html returns the people list page."""
        from lucos_photos_common.models import Person
        person = Person(display_name="Test Person")
        db_session.add(person)
        db_session.commit()

        response = authenticated_client.get("/people", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "lucos-navbar" in response.text

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

    def test_people_page_cards_link_to_person(self, authenticated_client, db_session):
        """Person cards on the people list page link to the individual person page."""
        from lucos_photos_common.models import Person
        person = Person(display_name="Test Person")
        db_session.add(person)
        db_session.commit()

        response = authenticated_client.get("/people", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert f'href="/people/{person.id}"' in response.text

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


# ---------------------------------------------------------------------------
# Homepage tests
# ---------------------------------------------------------------------------

class TestHomepage:
    def test_homepage_returns_html(self, authenticated_client):
        """GET / returns server-rendered HTML."""
        response = authenticated_client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "lucos-navbar" in response.text

    def test_homepage_shows_photo_count(self, authenticated_client, db_session):
        """Homepage renders photo count as a section heading."""
        from lucos_photos_common.models import MediaItem, ProcessingStatus, ProcessingState
        photo = MediaItem(sha256_hash="a" * 64, file_extension="jpg", media_type="photo")
        db_session.add(photo)
        db_session.flush()
        db_session.add(ProcessingStatus(photo_id=photo.id, state=ProcessingState.complete))
        db_session.commit()

        response = authenticated_client.get("/")
        assert response.status_code == 200
        assert "1 Photos" in response.text

    def test_homepage_shows_people_count(self, authenticated_client, db_session):
        """Homepage renders people count as a section heading."""
        from lucos_photos_common.models import Person
        person = Person(display_name="Test Person")
        db_session.add(person)
        db_session.commit()

        response = authenticated_client.get("/")
        assert response.status_code == 200
        assert "1 People" in response.text

    def test_homepage_photos_link_to_photos_page(self, authenticated_client):
        """Photos section heading and 'All Photos' link point to /photos."""
        response = authenticated_client.get("/")
        assert response.status_code == 200
        assert 'href="/photos"' in response.text

    def test_homepage_people_link_to_people_page(self, authenticated_client):
        """People section heading and 'All People' link point to /people."""
        response = authenticated_client.get("/")
        assert response.status_code == 200
        assert 'href="/people"' in response.text

    def test_homepage_shows_recent_photo_thumbnails(self, authenticated_client, db_session):
        """Homepage shows thumbnail links for processed photos."""
        from lucos_photos_common.models import MediaItem, ProcessingStatus, ProcessingState
        photo = MediaItem(sha256_hash="b" * 64, file_extension="jpg", media_type="photo")
        db_session.add(photo)
        db_session.flush()
        db_session.add(ProcessingStatus(photo_id=photo.id, state=ProcessingState.complete))
        db_session.commit()

        response = authenticated_client.get("/")
        assert response.status_code == 200
        assert str(photo.id) in response.text

    def test_homepage_shows_person_profile_pictures(self, authenticated_client, db_session):
        """Homepage shows person links for top people."""
        from lucos_photos_common.models import Person
        person = Person(display_name="Alice")
        db_session.add(person)
        db_session.commit()

        response = authenticated_client.get("/")
        assert response.status_code == 200
        assert str(person.id) in response.text

    def test_homepage_has_all_photos_link(self, authenticated_client):
        """Homepage includes an 'All Photos' link styled as a pagination link."""
        response = authenticated_client.get("/")
        assert response.status_code == 200
        assert "All Photos" in response.text
        assert "All People" in response.text

    def test_homepage_nav_has_home_photos_people(self, authenticated_client):
        """Nav includes Home, Photos, and People links."""
        response = authenticated_client.get("/")
        assert response.status_code == 200
        assert 'href="/"' in response.text
        assert 'href="/photos"' in response.text
        assert 'href="/people"' in response.text

    def test_homepage_excludes_unprocessed_photos(self, authenticated_client, db_session):
        """Photos with pending processing status must not appear on the homepage."""
        from lucos_photos_common.models import MediaItem, ProcessingStatus, ProcessingState
        photo = MediaItem(sha256_hash="c" * 64, file_extension="jpg", media_type="photo")
        db_session.add(photo)
        db_session.flush()
        db_session.add(ProcessingStatus(photo_id=photo.id, state=ProcessingState.pending))
        db_session.commit()

        response = authenticated_client.get("/")
        assert response.status_code == 200
        # Photo count should be 0 since only pending photos exist
        assert "0 Photos" in response.text


# ---------------------------------------------------------------------------
# /photos HTML page tests
# ---------------------------------------------------------------------------

class TestPhotosPageHtml:
    def test_photos_page_requires_auth(self, client):
        """Unauthenticated requests to GET /photos (HTML) must redirect to auth."""
        response = client.get("/photos", headers={"Accept": "text/html"}, follow_redirects=False)
        assert response.status_code == 302
        assert "auth.l42.eu" in response.headers["location"]

    def test_photos_page_returns_html(self, authenticated_client):
        """GET /photos with Accept: text/html returns the photos list page."""
        response = authenticated_client.get("/photos", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "lucos-navbar" in response.text

    def test_photos_page_shows_photo_count(self, authenticated_client, db_session):
        """GET /photos HTML page shows total count in heading."""
        from lucos_photos_common.models import MediaItem, ProcessingStatus, ProcessingState
        photo = MediaItem(sha256_hash="d" * 64, file_extension="jpg", media_type="photo")
        db_session.add(photo)
        db_session.flush()
        db_session.add(ProcessingStatus(photo_id=photo.id, state=ProcessingState.complete))
        db_session.commit()

        response = authenticated_client.get("/photos", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert "1 Photos" in response.text

    def test_photos_page_marks_photos_as_current(self, authenticated_client):
        """The Photos nav link should be marked as current-page on /photos."""
        response = authenticated_client.get("/photos", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert "current-page" in response.text

    def test_photos_json_still_works(self, authenticated_client):
        """GET /photos with Accept: application/json returns JSON."""
        response = authenticated_client.get("/photos", headers={"Accept": "application/json"})
        assert response.status_code == 200
        body = response.json()
        assert "photos" in body
        assert "total" in body

    def test_photos_html_has_vary_accept(self, authenticated_client):
        """HTML response from /photos must include Vary: Accept."""
        response = authenticated_client.get("/photos", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert response.headers.get("vary") == "Accept"

    def test_photos_json_has_vary_accept(self, authenticated_client):
        """JSON response from /photos must include Vary: Accept."""
        response = authenticated_client.get("/photos", headers={"Accept": "application/json"})
        assert response.status_code == 200
        assert response.headers.get("vary") == "Accept"

    def test_photos_page_includes_lightbox_markup(self, authenticated_client):
        """The /photos page must include lightbox overlay HTML for the photo viewer."""
        response = authenticated_client.get("/photos", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert 'id="lightbox"' in response.text
        assert 'id="lightbox-close"' in response.text
        assert 'id="lightbox-prev"' in response.text
        assert 'id="lightbox-next"' in response.text

    def test_photos_page_includes_lightbox_script(self, authenticated_client):
        """The /photos page must include the shared lightbox.js script."""
        response = authenticated_client.get("/photos", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert 'src="/lightbox.js"' in response.text
        assert "initLightbox()" in response.text

    def test_photos_page_cards_have_lightbox_data_attrs(self, authenticated_client, db_session):
        """Photo cards must include data attributes for the lightbox to use."""
        from lucos_photos_common.models import MediaItem, ProcessingStatus, ProcessingState
        photo = MediaItem(sha256_hash="lb" * 32, file_extension="jpg", media_type="photo")
        db_session.add(photo)
        db_session.flush()
        db_session.add(ProcessingStatus(photo_id=photo.id, state=ProcessingState.complete))
        db_session.commit()

        response = authenticated_client.get("/photos", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert 'data-original-url=' in response.text
        assert 'data-media-type="photo"' in response.text
        assert f'data-photo-id="{photo.id}"' in response.text

    def test_photos_page_video_has_play_overlay(self, authenticated_client, db_session):
        """Video items in the photo grid must show a play overlay icon."""
        from lucos_photos_common.models import MediaItem, ProcessingStatus, ProcessingState
        video = MediaItem(sha256_hash="vd" * 32, file_extension="mp4", media_type="video")
        db_session.add(video)
        db_session.flush()
        db_session.add(ProcessingStatus(photo_id=video.id, state=ProcessingState.complete))
        db_session.commit()

        response = authenticated_client.get("/photos", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert 'data-media-type="video"' in response.text
        assert 'class="play-overlay"' in response.text
