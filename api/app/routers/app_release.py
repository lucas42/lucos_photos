"""Routes for Android app release information."""

import os
import time
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.templating import Jinja2Templates

from app.auth import verify_session_or_key

router = APIRouter()

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

GITHUB_RELEASES_API_URL = "https://api.github.com/repos/lucas42/lucos_photos_android/releases/latest"
GITHUB_RELEASES_LIST_URL = "https://api.github.com/repos/lucas42/lucos_photos_android/releases?per_page=10"
_APP_LATEST_CACHE: dict = {"data": None, "fetched_at": 0.0}
_APP_LATEST_CACHE_TTL = 300  # 5 minutes
_APP_LATEST_ERROR_CACHE: dict = {"error": None, "fetched_at": 0.0}
_APP_LATEST_ERROR_CACHE_TTL = 60  # 1 minute

_GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def _extract_apk_result(release: dict, updating: bool = False) -> dict | None:
    """Extract version/download_url/released_at from a GitHub release dict.

    Returns None if the release has no APK asset.
    Includes 'updating: true' when a newer release is being published.
    """
    assets = release.get("assets", [])
    apk_asset = next((a for a in assets if a.get("name", "").endswith(".apk")), None)
    if not apk_asset:
        return None

    tag_name: str = release.get("tag_name", "")
    version = tag_name.lstrip("v") if tag_name else tag_name
    released_at: str = release.get("published_at") or release.get("created_at", "")
    download_url: str = apk_asset.get("browser_download_url", "")

    result: dict = {
        "version": version,
        "download_url": download_url,
        "released_at": released_at,
    }
    if updating:
        result["updating"] = True
    return result


async def _fetch_latest_app_release() -> dict:
    """Fetch the latest release from GitHub Releases API, with a 5-minute in-memory cache.

    Returns a dict with version, download_url, and released_at.

    If the latest GitHub release has no APK yet (i.e. a release is currently being
    published), falls back to the most recent release that does have an APK, and
    includes 'updating: true' in the response so the UI can signal this to the user.

    Raises HTTPException 404 if no release with an APK is found anywhere.
    Raises HTTPException 502 if the GitHub API is unreachable.

    Both successful and error results are cached to avoid hammering GitHub during
    transient failures. Successful results are cached for 5 minutes; errors for 60 seconds.
    """
    now = time.monotonic()
    if _APP_LATEST_CACHE["data"] is not None and now - _APP_LATEST_CACHE["fetched_at"] < _APP_LATEST_CACHE_TTL:
        return _APP_LATEST_CACHE["data"]

    if _APP_LATEST_ERROR_CACHE["error"] is not None and now - _APP_LATEST_ERROR_CACHE["fetched_at"] < _APP_LATEST_ERROR_CACHE_TTL:
        cached_error = _APP_LATEST_ERROR_CACHE["error"]
        raise HTTPException(status_code=cached_error["status_code"], detail=cached_error["detail"])

    def _cache_error_and_raise(status_code: int, detail: str) -> None:
        _APP_LATEST_ERROR_CACHE["error"] = {"status_code": status_code, "detail": detail}
        _APP_LATEST_ERROR_CACHE["fetched_at"] = now
        raise HTTPException(status_code=status_code, detail=detail)

    try:
        async with httpx.AsyncClient(headers={"User-Agent": os.environ.get("SYSTEM", "")}) as client:
            resp = await client.get(
                GITHUB_RELEASES_API_URL,
                headers=_GITHUB_HEADERS,
                timeout=5.0,
            )
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        _cache_error_and_raise(502, f"Failed to reach GitHub Releases API: {exc}")

    if resp.status_code == 404:
        _cache_error_and_raise(404, "No app releases found")

    if not resp.is_success:
        _cache_error_and_raise(502, f"GitHub Releases API returned {resp.status_code}")

    release = resp.json()
    result = _extract_apk_result(release)

    if result is None:
        # The latest release exists but has no APK yet — a release is in progress.
        # Fall back to the most recent release that does have an APK so users can
        # still download a working version while the new one is being published.
        try:
            async with httpx.AsyncClient(headers={"User-Agent": os.environ.get("SYSTEM", "")}) as client:
                list_resp = await client.get(
                    GITHUB_RELEASES_LIST_URL,
                    headers=_GITHUB_HEADERS,
                    timeout=5.0,
                )
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            _cache_error_and_raise(502, f"Failed to reach GitHub Releases API: {exc}")

        if not list_resp.is_success:
            _cache_error_and_raise(502, f"GitHub Releases API returned {list_resp.status_code}")

        releases = list_resp.json()
        for candidate in releases:
            result = _extract_apk_result(candidate, updating=True)
            if result is not None:
                break

        if result is None:
            _cache_error_and_raise(404, "No APK asset found in any recent release")

    _APP_LATEST_CACHE["data"] = result
    _APP_LATEST_CACHE["fetched_at"] = now
    return result


@router.get("/api/app/latest")
async def get_app_latest(_: Annotated[None, Depends(verify_session_or_key)]):
    """Return the version number and download URL of the latest Android app release.

    Fetches from GitHub Releases API and caches the result for 5 minutes.
    Returns 404 if no release exists yet, or 502 if the GitHub API is unreachable.

    Accepts either session cookie auth (browser) or key auth (Android app).
    """
    return await _fetch_latest_app_release()


@router.get("/app", include_in_schema=False)
async def app_page(request: Request, _: Annotated[None, Depends(verify_session_or_key)]):
    return templates.TemplateResponse(request, "app.html", {"current_page": "app"})
