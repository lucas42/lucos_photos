"""Authentication and session management for the lucos_photos API."""

import os
from typing import Annotated
from urllib.parse import quote, urlencode, urlparse

import httpx
from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from fastapi.responses import RedirectResponse

AUTH_DOMAIN = "https://auth.l42.eu"

WWW_AUTHENTICATE = {"WWW-Authenticate": 'Bearer realm="lucos_photos"'}


def safe_path(path: str, fallback: str = "/") -> str:
    """Validate a URL path to prevent open redirects.

    Only allows relative paths (no scheme or netloc). Anything that would be
    interpreted as an external URL — e.g. //evil.com (protocol-relative) or
    https://evil.com — is rejected and replaced with the fallback.

    This should be applied to user-influenced path components *before* they are
    combined with APP_ORIGIN, so that an empty APP_ORIGIN cannot be combined
    with a crafted path to produce an external redirect.
    """
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc:
        return fallback
    return path


class _RedirectWithCookie(Exception):
    """Raised inside verify_session to deliver a redirect response with a Set-Cookie header.

    FastAPI's dependency injection doesn't support returning responses directly from
    dependencies, so we raise this exception and catch it in a middleware.
    """
    def __init__(self, response: RedirectResponse):
        self.response = response


def _is_valid_key(authorization: str | None) -> bool:
    """Return True if the Authorization header contains a valid CLIENT_KEYS entry.

    Accepts both 'Bearer <token>' and 'key <token>' schemes (the Android app uses
    the latter). Returns False if the header is absent, malformed, or the token is
    not in CLIENT_KEYS.
    """
    if not authorization:
        return False
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() not in ("bearer", "key"):
        return False
    token = parts[1]
    client_keys_str = os.environ.get("CLIENT_KEYS", "")
    valid_keys = {entry.split("=", 1)[1] for entry in client_keys_str.split(";") if "=" in entry}
    return token in valid_keys


def verify_key(authorization: Annotated[str | None, Header()] = None):
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required", headers=WWW_AUTHENTICATE)
    if not _is_valid_key(authorization):
        raise HTTPException(status_code=401, detail="Invalid key", headers=WWW_AUTHENTICATE)


def verify_key_if_present(authorization: Annotated[str | None, Header()] = None):
    """Accept requests without an Authorization header, but reject invalid tokens.

    Used during the Phase 1 migration window before Loganne starts sending
    Bearer tokens — allows zero-downtime rollout by not breaking unauthenticated
    callers that haven't been updated yet.
    """
    if authorization is not None and not _is_valid_key(authorization):
        raise HTTPException(status_code=401, detail="Invalid key", headers=WWW_AUTHENTICATE)


async def verify_session_or_key(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    auth_token: Annotated[str | None, Cookie()] = None,
):
    """Accept either key auth (Authorization: key/Bearer <token>) or a session cookie.

    Used for endpoints that need to be callable both from the browser (cookie auth)
    and from machine-to-machine clients like the Android app (key auth).
    If an Authorization header is present and contains a valid key, auth succeeds
    immediately. Otherwise, falls through to session cookie validation.

    Note: if an Authorization header is present but the key is invalid, the request
    still falls through to session cookie validation. This is intentional — a browser
    user with a stale or unrelated Authorization header set (e.g. from a dev tool)
    will still be authenticated via cookie rather than being locked out.
    """
    if _is_valid_key(authorization):
        return  # key auth succeeded

    await verify_session(request, auth_token)


async def verify_session(request: Request, auth_token: Annotated[str | None, Cookie()] = None):
    """Validate a user session via the lucos_authentication service.

    - If a ?token= query parameter is present (auth callback), validate it, set a
      cookie on the photos domain, and redirect to strip the token from the URL.
    - Browser requests (Accept: text/html) without a token are redirected to the
      auth service login page.
    - API requests receive a 401 JSON response.
    """
    # Check for token in query parameter (auth service callback)
    query_token = request.query_params.get("token")
    if query_token:
        data = await _validate_token_with_auth_service(query_token)
        if data and data.get("id"):
            # Strip the token from the URL so it doesn't linger in browser history
            app_origin = os.environ.get("APP_ORIGIN", "")
            # Validate the path before combining with APP_ORIGIN to prevent open redirects:
            # a crafted path like //evil.com would become a valid external redirect if
            # APP_ORIGIN is empty.
            path = safe_path(request.url.path)
            clean_url = f"{app_origin}{path}"
            # Preserve any other query params except 'token'
            other_params = {k: v for k, v in request.query_params.items() if k != "token"}
            if other_params:
                clean_url += "?" + urlencode(other_params)
            response = RedirectResponse(url=clean_url, status_code=status.HTTP_302_FOUND)
            response.set_cookie(
                key="auth_token",
                value=query_token,
                httponly=True,
                secure=True,
                samesite="lax",
            )
            raise _RedirectWithCookie(response)
        _auth_challenge(request)

    if not auth_token:
        _auth_challenge(request)

    data = await _validate_token_with_auth_service(auth_token)
    if not data or not data.get("id"):
        _auth_challenge(request)


async def _validate_token_with_auth_service(token: str) -> dict | None:
    """Call auth.l42.eu/data?token=<token> and return the JSON payload, or None on failure."""
    try:
        async with httpx.AsyncClient(headers={"User-Agent": os.environ.get("SYSTEM", "")}) as client:
            resp = await client.get(
                f"{AUTH_DOMAIN}/data",
                params={"token": token},
                timeout=5.0,
            )
            resp.raise_for_status()
            return resp.json()
    except (httpx.HTTPError, httpx.TimeoutException):
        return None


def _auth_challenge(request: Request):
    """Return redirect or 401 depending on whether the client is a browser."""
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        app_origin = os.environ.get("APP_ORIGIN", "")
        redirect_uri = quote(f"{app_origin}{request.url.path}", safe="")
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": f"{AUTH_DOMAIN}/authenticate?redirect_uri={redirect_uri}"},
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": f'Bearer realm="{AUTH_DOMAIN}"'},
    )
