"""Authentication and session management for the lucos_photos API."""

import logging
import os
import re
from typing import Annotated
from urllib.parse import quote, urlparse

import jwt
from fastapi import Cookie, Header, HTTPException, Request, status

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Aithne configuration
# AITHNE_ORIGIN: browser-facing identity — used for iss validation AND the
#   login redirect.  Never overridden by AITHNE_JWKS_URL (contract §1 guard-rail).
# AITHNE_JWKS_URL: server-side JWKS fetch address only.  Unset in production
#   (AITHNE_ORIGIN is reachable from containers there).  In dev, bridge-network
#   containers can't reach the browser-facing localhost, so this points the
#   key-fetch at a container-reachable address instead.
# ---------------------------------------------------------------------------

AITHNE_ORIGIN = os.environ.get("AITHNE_ORIGIN", "https://aithne.l42.eu")
AITHNE_JWKS_URL = os.environ.get("AITHNE_JWKS_URL") or f"{AITHNE_ORIGIN}/.well-known/jwks.json"
AITHNE_LOGIN_URL = f"{AITHNE_ORIGIN}/auth/login"

APP_ORIGIN = os.environ.get("APP_ORIGIN", "")

REQUIRED_SCOPE = "photos:use"
CLOCK_SKEW = 30  # seconds, per contract §"Clock skew"

WWW_AUTHENTICATE = {"WWW-Authenticate": 'Bearer realm="lucos_photos"'}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _sanitize_for_log(text: str) -> str:
    """Strip C0 controls and DEL from a string before logging.

    ``kid`` is an attacker-controlled JWT header field that appears verbatim in
    PyJWKClientError messages.  Logging an unsanitised value lets an attacker
    inject arbitrary content (including newlines and terminal control sequences)
    into the log stream.  Strip before any log call that may carry ``kid``.
    Contract §2 (lucas42/lucos_arachne#646).
    """
    return re.sub(r"[\x00-\x1f\x7f]", "", str(text))


# ---------------------------------------------------------------------------
# Resilient JWKS client — serve last-known-good on fetch failure
# ---------------------------------------------------------------------------


class _ResilientJWKSClient(jwt.PyJWKClient):
    """PyJWKClient that falls back to last-known-good keys on a JWKS fetch failure.

    ``PyJWKClient.get_jwk_set`` raises ``PyJWKClientConnectionError`` when the
    JWKS endpoint is unreachable, which would cause a 401 storm on every token
    during an aithne outage.  This subclass overrides ``get_jwk_set`` to catch
    that specific error, log a WARNING (contract §"Log a failed JWKS fetch"), and
    return the previously cached key set instead.

    Maintains its own ``self._last_known_good`` snapshot, updated only on a
    successful fetch — deliberately NOT relying on the parent class's own
    ``jwk_set_cache``. An earlier version of this class read
    ``self.jwk_set_data``, an attribute that does not exist on PyJWT>=2.13.0's
    ``PyJWKClient`` (it stores its cache in ``self.jwk_set_cache``, a
    ``JWKSetCache`` object) — so that fallback silently raised
    ``AttributeError`` instead of serving stale keys, turning a would-be clean
    rejection into an unhandled exception.

    The parent's own cache isn't a substitute fallback source either, though
    for a narrower reason than a self-wiping bug: PyJWT 2.13.0 fixed exactly
    that failure mode as a security patch (GHSA-fhv5-28vv-h8m8) —
    ``fetch_data()`` only calls ``self.jwk_set_cache.put(...)`` on the
    success path now, so a failed fetch does *not* clear an existing cache
    entry. The real limitation is TTL: ``JWKSetCache.get()`` returns ``None``
    once ``lifespan`` (300s here) has elapsed, regardless of whether the most
    recent fetch succeeded or failed. So once the cache has gone stale by
    time alone, ``get_jwk_set`` must call ``fetch_data()`` again — and if
    aithne is down at that moment, there is nothing left in the parent's
    cache to fall back to. An independent, non-expiring snapshot is what
    actually survives past that TTL boundary.

    Per contract §"Serve last-known-good on a failed refresh".
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._last_known_good: "jwt.PyJWKSet | None" = None

    def get_jwk_set(self, refresh: bool = False) -> "jwt.PyJWKSet":  # type: ignore[override]
        try:
            jwk_set = super().get_jwk_set(refresh=refresh)
            self._last_known_good = jwk_set
            return jwk_set
        except jwt.exceptions.PyJWKClientConnectionError as exc:
            safe_msg = _sanitize_for_log(str(exc))
            if self._last_known_good is not None:
                log.warning("JWKS fetch failed: %s — serving last-known-good key set", safe_msg)
                return self._last_known_good
            log.error("JWKS fetch failed: %s — no cached key set available, cannot authenticate", safe_msg)
            raise


# Module-level singleton (lazy-initialised so env vars are picked up correctly)
_jwks_client: _ResilientJWKSClient | None = None


def _get_jwks_client() -> _ResilientJWKSClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = _ResilientJWKSClient(AITHNE_JWKS_URL, cache_keys=True, lifespan=300)
    return _jwks_client


def _set_jwks_client(client: "_ResilientJWKSClient | None") -> None:
    """Replace the module-level JWKS client.  For testing only."""
    global _jwks_client
    _jwks_client = client


# ---------------------------------------------------------------------------
# Scope / authorisation check
# ---------------------------------------------------------------------------


def has_photos_access(scopes: list) -> bool:
    """Return True if the scope list grants access to lucos_photos.

    Accepts ``photos:use`` for all principals (contract §6 / ADR-0001 §6).
    Accepts the estate-wide ``render-ui`` bypass in the development environment
    only — lets lucos-ux take page snapshots without a full aithne session.
    The production guard is checked on every call so that tests can control the
    environment by setting ``ENVIRONMENT`` directly (same pattern as arachne canary).
    """
    if REQUIRED_SCOPE in scopes:
        return True
    if os.environ.get("ENVIRONMENT", "production") == "development" and "render-ui" in scopes:
        return True
    return False


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------


def verify_aithne_token(token: str) -> "dict | None":
    """Verify an ``aithne_session`` JWT and return its payload, or None on failure.

    Validates:
    - Signature via JWKS key matching the token's ``kid`` (§1–2)
    - Algorithm pinned to ES256 — never trust the token-header ``alg`` (§3 / lucos_arachne#637)
    - ``iss`` == AITHNE_ORIGIN, ``aud`` contains ``l42.eu``, ``exp``/``iat`` with
      30-second clock-skew tolerance (§4)

    ``principal_class`` is informational only, not an authorisation input —
    an absent/unrecognised value does not cause rejection (§5 redesign,
    lucas42/lucos_aithne#268).

    Returns None for any validation failure so callers get a uniform "unauthenticated"
    signal.  Sanitises all log output to prevent log injection (§2 / lucos_arachne#646).
    """
    try:
        client = _get_jwks_client()
        signing_key = client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256"],  # pin — never accept what the token header says (§3)
            audience="l42.eu",
            issuer=AITHNE_ORIGIN,  # always AITHNE_ORIGIN, never AITHNE_JWKS_URL (§1 guard-rail)
            leeway=CLOCK_SKEW,
        )
        # principal_class is informational only — authorisation is enforced
        # purely by scope (ADR-0001 §6). Do not reject on an absent or
        # unrecognised principal_class (§5 redesign, lucas42/lucos_aithne#268).
        # No other logging site reads it in this repo, so there is nothing
        # to preserve a read for.
        return payload
    except jwt.exceptions.PyJWKClientError as exc:
        # Covers connection errors (already logged at WARNING by _ResilientJWKSClient)
        # and unknown-kid errors after a refresh attempt.
        log.error("JWKS client error: %s", _sanitize_for_log(str(exc)))
        return None
    except jwt.exceptions.InvalidTokenError as exc:
        # Expected noise: expired tokens, bad signature, wrong iss/aud.  INFO only.
        log.info("JWT verification failed: %s", _sanitize_for_log(str(exc)))
        return None
    except Exception as exc:
        log.error("Unexpected JWT verification error: %s", _sanitize_for_log(str(exc)))
        return None


# ---------------------------------------------------------------------------
# CSRF protection (contract §"CSRF protection required")
# ---------------------------------------------------------------------------


def is_allowed_origin(origin: str | None) -> bool:
    """Return True if ``origin`` is ``l42.eu`` or a subdomain thereof.

    Used by both CSRF protection (_check_csrf) and WebSocket Origin checks
    to keep the allow-list in one place.  Returns False for absent/empty
    origins — callers decide whether a missing Origin header is acceptable
    for their protocol context.
    """
    if not origin:
        return False
    parsed = urlparse(origin)
    host = parsed.hostname or ""
    return host == "l42.eu" or host.endswith(".l42.eu")


def _check_csrf(request: Request) -> None:
    """Reject cross-origin state-mutating requests that lack CSRF protection.

    ``aithne_session`` uses ``SameSite=None``, so browsers attach it to all
    cross-origin requests — including CSRF-triggered ones.  For cookie-
    authenticated write operations, require either:

    - ``X-Requested-With: XMLHttpRequest`` — browsers don't send custom headers
      on CSRF-triggered requests, distinguishing legitimate AJAX calls.
    - ``Origin`` header matching ``l42.eu`` or ``*.l42.eu``.

    Raises ``HTTPException(403)`` if neither marker is present.

    Bearer/key-authenticated endpoints are unaffected — browsers don't attach
    ``Authorization`` headers to CSRF-triggered requests.
    """
    if request.headers.get("x-requested-with", "").lower() == "xmlhttprequest":
        return
    if is_allowed_origin(request.headers.get("origin")):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="CSRF check failed: missing X-Requested-With header or valid l42.eu Origin",
    )


# ---------------------------------------------------------------------------
# M2M key auth
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Session verification — three-branch pattern (contract C2)
# ---------------------------------------------------------------------------


async def _verify_aithne_session(request: Request, aithne_session: "str | None") -> None:
    """Validate an ``aithne_session`` JWT using the three-branch estate pattern:

    1. Valid JWT **and** ``photos:use`` scope (or dev ``render-ui`` bypass) → return.
    2. Valid JWT, **missing/wrong scope** → raise HTTPException(403).
       Do NOT redirect to login here: re-login yields the same scopeless token,
       creating an infinite loop.  The user needs a grant, not a re-auth.
    3. No cookie / expired / invalid token → raise HTTPException(302) to the
       aithne login page.

    ``/_info`` is exempt because that route carries no auth ``Depends`` — this
    function is never called for monitoring requests (FastAPI per-route wiring).

    Open-redirect guard: ``?next=`` is built from ``APP_ORIGIN`` (env var) combined
    with ``request.url.path`` validated through ``safe_path()``, never from a
    user-supplied parameter.  ``safe_path()`` rejects any path carrying a scheme or
    netloc before it is combined with ``APP_ORIGIN``, so a crafted path cannot
    produce an external redirect.  The resulting full URL is percent-encoded before
    embedding in the ``next=`` query parameter.
    """
    if aithne_session:
        payload = verify_aithne_token(aithne_session)
        if payload is not None:
            scopes = payload.get("scopes", [])
            if has_photos_access(scopes):
                return  # branch 1 — proceed

            # Branch 2: signed in, no photos:use grant
            log.warning(
                "JWT missing required %s scope for sub=%s",
                REQUIRED_SCOPE,
                _sanitize_for_log(str(payload.get("sub", ""))),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires the {REQUIRED_SCOPE} scope.",
            )

    # Branch 3: no session cookie, or token failed verification.
    # Build a full absolute URL for ?next= so aithne redirects back to photos,
    # not to itself.  safe_path() validates the path before combining with
    # APP_ORIGIN; quote() percent-encodes the whole URL for safe embedding.
    path = safe_path(request.url.path)
    query = request.url.query
    full_next_url = f"{APP_ORIGIN}{path}"
    if query:
        full_next_url = f"{full_next_url}?{query}"
    login_url = f"{AITHNE_LOGIN_URL}?next={quote(full_next_url, safe='')}"
    raise HTTPException(
        status_code=status.HTTP_302_FOUND,
        headers={"Location": login_url},
    )


async def verify_session_or_key(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    aithne_session: Annotated[str | None, Cookie()] = None,
) -> None:
    """Accept either M2M key auth or a valid aithne_session cookie.

    Key auth (Authorization: Bearer/key <token>) → auth succeeds immediately;
    no CSRF check needed (browsers don't attach Authorization on CSRF requests).

    Cookie auth → CSRF check required on write methods (POST/PUT/DELETE/PATCH),
    then three-branch aithne session validation.

    An invalid Authorization header does NOT hard-fail — it falls through to the
    cookie path so that a browser user with a stale dev-tool Authorization header
    still authenticates via cookie rather than being locked out.
    """
    if _is_valid_key(authorization):
        return  # M2M key auth — skip CSRF

    # Cookie path — CSRF check for state-mutating methods (C5)
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        _check_csrf(request)

    await _verify_aithne_session(request, aithne_session)
