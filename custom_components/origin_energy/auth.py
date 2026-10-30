"""Mint Origin Energy API tokens from an imported browser session.

Origin puts Kasada bot defence in front of exactly one endpoint -- the
interactive password form, ``POST /usernamepassword/login`` -- which answers
any non-browser client with a body-less HTTP 429 regardless of credentials,
IP or headers. Everything else in the chain is unprotected: the silent PKCE
authorization (``/authorize?prompt=none``), the token exchange
(``/oauth/token``) and the GraphQL API all accept an ordinary HTTP client.

So this module doesn't automate the password form at all. You sign in once
in a real browser -- precisely the flow that endpoint exists to protect --
and import the resulting Auth0 SSO cookie. From then on we do exactly what
Origin's own web app does on every page load: a silent code+PKCE round trip
that mints a fresh one-hour access token.

Auth0 re-issues that SSO cookie with a rolling ~100-day expiry each time the
silent call succeeds, so as long as the integration polls well inside that
window the imported session keeps renewing itself and never has to be
replaced by hand.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass

from aiohttp import ClientSession
from yarl import URL

from .const import (
    AUDIENCE,
    AUTH0_SPA_CLIENT,
    AUTH_DOMAIN,
    AUTHORIZE_URL,
    BROWSER_HEADERS,
    CLIENT_ID,
    REDIRECT_URI,
    SCOPE,
    SESSION_COOKIE_NAME,
    TOKEN_URL,
)

AUTH_BASE = URL(f"https://{AUTH_DOMAIN}/")


class OriginEnergyAuthError(Exception):
    """Raised when tokens could not be obtained."""


class OriginEnergyBlocked(OriginEnergyAuthError):
    """Origin's WAF refused the request before it reached Auth0.

    Distinct from a bad cookie: the session may be perfectly valid and
    still be turned away, so telling the user to copy it again is wrong.
    """


class OriginEnergySessionExpired(OriginEnergyAuthError):
    """The imported browser session is no longer valid.

    Recoverable only by signing in again in a browser and importing a fresh
    cookie, so callers should surface this as a re-auth prompt rather than a
    transient failure.
    """


@dataclass
class OriginEnergyTokens:
    """Tokens returned by the silent code exchange."""

    access_token: str
    id_token: str
    expires_in: int


def _code_verifier() -> str:
    return secrets.token_urlsafe(64)[:128]


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def seed_session_cookie(session: ClientSession, cookie_value: str) -> None:
    """Load the imported Auth0 SSO cookie into the session's cookie jar."""
    session.cookie_jar.update_cookies(
        {SESSION_COOKIE_NAME: cookie_value}, response_url=AUTH_BASE
    )


def current_session_cookie(session: ClientSession) -> str | None:
    """Read the SSO cookie back out, post-refresh, so it can be persisted.

    Auth0 rolls the cookie's expiry forward on every successful silent
    authorization; storing the value it hands back is what keeps the
    imported session alive indefinitely.
    """
    cookie = session.cookie_jar.filter_cookies(AUTH_BASE).get(SESSION_COOKIE_NAME)
    return cookie.value if cookie else None


async def async_get_tokens(session: ClientSession) -> OriginEnergyTokens:
    """Run the silent PKCE exchange, using whatever session cookie is loaded."""
    verifier = _code_verifier()
    async with session.get(
        AUTHORIZE_URL,
        # Headers go on the request, not the session: Home Assistant
        # overwrites a session's defaults with its own User-Agent, and this
        # endpoint is behind a WAF that answers non-browser clients with 403.
        headers=BROWSER_HEADERS,
        params={
            "client_id": CLIENT_ID,
            "audience": AUDIENCE,
            "scope": SCOPE,
            "redirect_uri": REDIRECT_URI,
            "issuer": AUTH_DOMAIN,
            "auth0Client": AUTH0_SPA_CLIENT,
            "remember_me": "true",
            "response_type": "code",
            "response_mode": "web_message",
            "state": secrets.token_urlsafe(24),
            "nonce": secrets.token_urlsafe(24),
            "code_challenge": _code_challenge(verifier),
            "code_challenge_method": "S256",
            "prompt": "none",
        },
    ) as resp:
        if resp.status in (403, 429):
            raise OriginEnergyBlocked(
                f"Origin refused the sign-in request (HTTP {resp.status}) before it "
                "reached Auth0. This is their bot protection, not a problem with "
                "your session -- the request did not look like it came from a "
                "browser."
            )
        if resp.status != 200:
            raise OriginEnergyAuthError(f"Silent auth failed: HTTP {resp.status}")
        text = await resp.text()

    # response_mode=web_message returns an HTML page that postMessage()s the
    # result back to the SPA; the payload is plain JSON inside the script.
    match = re.search(r'"code"\s*:\s*"([^"]+)"', text)
    if not match:
        error_match = re.search(r'"error"\s*:\s*"([^"]+)"', text)
        error = error_match.group(1) if error_match else None
        if error in ("login_required", "consent_required", "interaction_required"):
            raise OriginEnergySessionExpired(
                f"Origin Energy session is no longer valid ({error}). Sign in again "
                "in your browser and import a fresh session cookie."
            )
        raise OriginEnergyAuthError(
            f"Silent auth did not return a code ({error or 'no code in response'})"
        )

    async with session.post(
        TOKEN_URL,
        headers=BROWSER_HEADERS,
        json={
            "redirect_uri": REDIRECT_URI,
            "remember_me": True,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
            "code": match.group(1),
            "grant_type": "authorization_code",
        },
    ) as resp:
        if resp.status != 200:
            raise OriginEnergyAuthError(f"Token exchange failed: HTTP {resp.status}")
        token_data = await resp.json()

    return OriginEnergyTokens(
        access_token=token_data["access_token"],
        id_token=token_data["id_token"],
        expires_in=token_data["expires_in"],
    )


async def async_login_with_cookie(
    session: ClientSession, cookie_value: str
) -> OriginEnergyTokens:
    """Seed an imported session cookie and mint tokens from it."""
    seed_session_cookie(session, cookie_value)
    return await async_get_tokens(session)
