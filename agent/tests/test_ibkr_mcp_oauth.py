"""Deterministic mock-server tests for IBKR public-client MCP OAuth.

These tests prove protocol compatibility against an in-process authorization
server: honest client identity, RFC 7591 public-client registration, stable
loopback redirect, RFC 9728 resource_metadata discovery, and RFC 8414 issuer
alignment. They do **not** prove a live connection to api.ibkr.com.

The previous registration test asserted Mozilla / Origin headers and then
returned 403 from the mock. That showed the impersonation headers were sent,
not that they were necessary. This file replaces that with an honest-client
capture and an explicit "no browser impersonation" check.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest
from mcp.client.auth.exceptions import OAuthRegistrationError

from src.config.schema import MCPServerConfig
from src.tools.mcp import MCPServerAdapter
from src.trading.connectors.ibkr.oauth import (
    BROWSER_IMPERSONATION_MARKERS,
    IBKR_MCP_PUBLIC_URL,
    IBKR_OAUTH_CALLBACK_PORT,
    apply_honest_ibkr_client_headers,
    classify_unauthenticated_mcp_response,
    honest_user_agent,
    looks_like_access_block,
    normalize_ibkr_authorization_server,
    pinned_ibkr_oauth_metadata,
    probe_ibkr_mcp_public_unauthenticated,
    public_client_registration_metadata,
)

pytestmark = pytest.mark.unit

_MCP_URL = IBKR_MCP_PUBLIC_URL
_PRM_URL = f"{_MCP_URL}/.well-known/oauth-protected-resource"
_ROOT_PRM_URL = "https://api.ibkr.com/.well-known/oauth-protected-resource"
_AS_METADATA_URL = "https://api.ibkr.com/.well-known/oauth-authorization-server"
_AS_METADATA_OAUTH2_PATH_URL = (
    "https://api.ibkr.com/.well-known/oauth-authorization-server/oauth2"
)
_REGISTER_URL = "https://api.ibkr.com/oauth2/register"
_FALLBACK_REGISTER_URL = "https://api.ibkr.com/register"

_AS_METADATA = {
    "issuer": "https://api.ibkr.com",
    "authorization_endpoint": "https://api.ibkr.com/oauth2/authorize",
    "token_endpoint": "https://api.ibkr.com/oauth2/api/v1/token",
    "registration_endpoint": _REGISTER_URL,
    "code_challenge_methods_supported": ["S256"],
    "token_endpoint_auth_methods_supported": ["none"],
}


def _ibkr_auth(tmp_path):
    cfg = MCPServerConfig.model_validate(
        {
            "type": "streamableHttp",
            "url": _MCP_URL,
            "auth": {
                "type": "oauth",
                "scopes": ["mcp.read"],
                "client_name": "Vibe-Trading",
                "cacheDir": str(tmp_path),
            },
            "enabledTools": ["*"],
        }
    )
    return MCPServerAdapter("ibkr", cfg)._build_client().transport.auth


def _prm_document(authorization_servers: list[str]) -> dict[str, object]:
    return {
        "resource": _MCP_URL,
        "authorization_servers": authorization_servers,
        "scopes_supported": ["mcp.read", "mcp.write", "mcp.orders.submit"],
        "bearer_methods_supported": ["header"],
    }


def _run_registration(auth, handler) -> None:
    async def run() -> None:
        async with httpx.AsyncClient(
            auth=auth,
            transport=httpx.MockTransport(handler),
        ) as client:
            await client.get(
                _MCP_URL,
                headers={"Accept": "application/json, text/event-stream"},
            )

    asyncio.run(run())


def test_honest_user_agent_is_this_product_not_a_browser() -> None:
    ua = honest_user_agent()
    assert ua.startswith("Vibe-Trading/")
    assert all(marker not in ua for marker in BROWSER_IMPERSONATION_MARKERS)


def test_public_client_metadata_is_rfc7591_none() -> None:
    assert public_client_registration_metadata() == {"token_endpoint_auth_method": "none"}


def test_normalize_ibkr_authorization_server_maps_oauth2_alias_to_issuer() -> None:
    assert normalize_ibkr_authorization_server("https://api.ibkr.com/oauth2") == "https://api.ibkr.com"
    assert normalize_ibkr_authorization_server("https://api.ibkr.com/oauth2/") == "https://api.ibkr.com"
    assert normalize_ibkr_authorization_server("https://api.ibkr.com") == "https://api.ibkr.com"
    assert (
        normalize_ibkr_authorization_server("https://example.com/oauth2")
        == "https://example.com/oauth2"
    )


def test_pinned_metadata_uses_issuer_aligned_endpoints() -> None:
    metadata = pinned_ibkr_oauth_metadata()
    # AnyHttpUrl may render a root issuer with a trailing slash; the host and
    # path-aligned endpoints are what the token/register URLs must match.
    assert str(metadata.issuer).rstrip("/") == "https://api.ibkr.com"
    assert str(metadata.authorization_endpoint).rstrip("/") == "https://api.ibkr.com/oauth2/authorize"
    assert str(metadata.token_endpoint).rstrip("/") == "https://api.ibkr.com/oauth2/api/v1/token"
    assert str(metadata.registration_endpoint).rstrip("/") == _REGISTER_URL


def test_apply_honest_headers_strips_origin_and_refuses_mozilla() -> None:
    request = httpx.Request(
        "POST",
        _REGISTER_URL,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Origin": "https://api.ibkr.com",
            "Accept": "*/*",
        },
    )
    apply_honest_ibkr_client_headers(request)
    ua = request.headers["user-agent"]
    assert ua.startswith("Vibe-Trading/")
    assert "origin" not in request.headers
    assert request.headers["accept"] == "application/json"


def test_apply_honest_headers_leaves_mcp_accept_alone() -> None:
    request = httpx.Request(
        "GET",
        _MCP_URL,
        headers={"Accept": "application/json, text/event-stream"},
    )
    apply_honest_ibkr_client_headers(request)
    assert request.headers["accept"] == "application/json, text/event-stream"
    assert request.headers["user-agent"].startswith("Vibe-Trading/")


def test_ibkr_oauth_registration_is_honest_public_client(tmp_path) -> None:
    auth = _ibkr_auth(tmp_path)
    assert auth.redirect_port == IBKR_OAUTH_CALLBACK_PORT
    seen: dict[str, object] = {}
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if url == _MCP_URL:
            seen["mcp-accept"] = request.headers.get("accept")
            seen["mcp-user-agent"] = request.headers.get("user-agent")
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": (
                        f'Bearer resource_metadata="{_PRM_URL}"'
                    )
                },
                request=request,
            )
        if url == _PRM_URL:
            return httpx.Response(
                200,
                json=_prm_document(["https://api.ibkr.com"]),
                request=request,
            )
        if url == _ROOT_PRM_URL:
            seen["fetched-root-prm"] = True
            return httpx.Response(
                200,
                json=_prm_document(["https://api.ibkr.com/oauth2"]),
                request=request,
            )
        if url == _AS_METADATA_URL:
            seen["as-metadata"] = url
            return httpx.Response(200, json=_AS_METADATA, request=request)
        if url == _REGISTER_URL:
            body = json.loads(request.content)
            seen["user-agent"] = request.headers.get("user-agent")
            seen["origin"] = request.headers.get("origin")
            seen["accept"] = request.headers.get("accept")
            seen["token-endpoint-auth-method"] = body.get("token_endpoint_auth_method")
            seen["client-name"] = body.get("client_name") or body.get("clientName")
            seen["redirect-uris"] = body.get("redirect_uris") or body.get("redirectUris")
            seen["grant-types"] = body.get("grant_types") or body.get("grantTypes")
            return httpx.Response(403, text="stop", request=request)
        if url == _FALLBACK_REGISTER_URL:
            seen["fallback-register"] = True
            return httpx.Response(403, text="wrong-register", request=request)
        if url == _AS_METADATA_OAUTH2_PATH_URL:
            seen["poisoned-as-metadata"] = True
            return httpx.Response(403, text="<html>Access Denied</html>", request=request)
        return httpx.Response(404, request=request)

    with pytest.raises(OAuthRegistrationError):
        _run_registration(auth, handler)

    ua = str(seen["user-agent"])
    assert ua.startswith("Vibe-Trading/")
    assert all(marker not in ua for marker in BROWSER_IMPERSONATION_MARKERS)
    assert seen["origin"] is None
    assert seen["accept"] == "application/json"
    assert seen["mcp-accept"] == "application/json, text/event-stream"
    mcp_ua = str(seen["mcp-user-agent"])
    assert mcp_ua.startswith("Vibe-Trading/")
    assert seen["token-endpoint-auth-method"] == "none"
    assert seen["client-name"] == "Vibe-Trading"
    assert seen["redirect-uris"] == [f"http://localhost:{IBKR_OAUTH_CALLBACK_PORT}/callback"]
    assert "authorization_code" in seen["grant-types"]
    assert "refresh_token" in seen["grant-types"]
    assert _PRM_URL in requested
    assert seen.get("fetched-root-prm") is not True
    assert seen.get("poisoned-as-metadata") is not True
    assert seen.get("fallback-register") is not True
    assert seen["as-metadata"] == _AS_METADATA_URL


def test_ibkr_oauth_does_not_impersonate_a_browser_on_any_ibkr_request(tmp_path) -> None:
    auth = _ibkr_auth(tmp_path)
    captured_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.ibkr.com":
            captured_headers.append(
                {
                    "url": str(request.url),
                    "user-agent": request.headers.get("user-agent") or "",
                    "origin": request.headers.get("origin") or "",
                }
            )
        if str(request.url) == _MCP_URL:
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{_PRM_URL}"'},
                request=request,
            )
        if str(request.url) == _PRM_URL:
            return httpx.Response(
                200, json=_prm_document(["https://api.ibkr.com"]), request=request
            )
        if str(request.url) == _AS_METADATA_URL:
            return httpx.Response(200, json=_AS_METADATA, request=request)
        if str(request.url) == _REGISTER_URL:
            return httpx.Response(403, text="stop", request=request)
        return httpx.Response(404, request=request)

    with pytest.raises(OAuthRegistrationError):
        _run_registration(auth, handler)

    assert captured_headers
    for row in captured_headers:
        ua = row["user-agent"]
        assert ua.startswith("Vibe-Trading/"), row
        assert all(marker not in ua for marker in BROWSER_IMPERSONATION_MARKERS), row
        assert row["origin"] == "", row


def test_ibkr_oauth_normalizes_poisoned_oauth2_authorization_server(tmp_path) -> None:
    """Domain-root PRM has advertised ``.../oauth2``; the issuer is the origin.

    Using the path-suffixed identifier makes mcp>=1.30 request
    ``/.well-known/oauth-authorization-server/oauth2`` (403 HTML) and then fail
    RFC 8414 issuer matching. The honest client must register at
    ``/oauth2/register``, not the guessed ``/register``.
    """
    auth = _ibkr_auth(tmp_path)
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if url == _MCP_URL:
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{_PRM_URL}"'},
                request=request,
            )
        if url == _PRM_URL:
            return httpx.Response(
                200,
                json=_prm_document(["https://api.ibkr.com/oauth2"]),
                request=request,
            )
        if url == _AS_METADATA_OAUTH2_PATH_URL:
            return httpx.Response(
                403,
                headers={"content-type": "text/html"},
                text="<html>Access Denied</html>",
                request=request,
            )
        if url == _AS_METADATA_URL:
            return httpx.Response(200, json=_AS_METADATA, request=request)
        if url == _REGISTER_URL:
            return httpx.Response(403, text="stop", request=request)
        if url == _FALLBACK_REGISTER_URL:
            return httpx.Response(403, text="wrong-register", request=request)
        return httpx.Response(404, request=request)

    with pytest.raises(OAuthRegistrationError, match="stop"):
        _run_registration(auth, handler)

    assert _AS_METADATA_URL in requested
    assert _AS_METADATA_OAUTH2_PATH_URL not in requested
    assert _REGISTER_URL in requested
    assert _FALLBACK_REGISTER_URL not in requested


def test_ibkr_oauth_authorize_preflight_uses_honest_identity_and_accepts_301(
    tmp_path,
) -> None:
    auth = _ibkr_auth(tmp_path)
    seen: dict[str, str | None] = {}

    def client_factory(**kwargs):
        def handler(request: httpx.Request) -> httpx.Response:
            seen["user-agent"] = request.headers.get("user-agent")
            seen["origin"] = request.headers.get("origin")
            seen["accept"] = request.headers.get("accept")
            return httpx.Response(
                301,
                headers={"Location": "https://api.ibkr.com/login"},
                request=request,
            )

        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    auth.httpx_client_factory = client_factory
    with patch("webbrowser.open") as open_browser:
        asyncio.run(auth.redirect_handler("https://api.ibkr.com/oauth2/authorize"))

    open_browser.assert_called_once_with("https://api.ibkr.com/oauth2/authorize")
    ua = str(seen["user-agent"])
    assert ua.startswith("Vibe-Trading/")
    assert all(marker not in ua for marker in BROWSER_IMPERSONATION_MARKERS)
    assert seen["origin"] is None
    assert seen["accept"] == "application/json"


def test_ibkr_oauth_authorize_preflight_does_not_retry_a_403(tmp_path) -> None:
    auth = _ibkr_auth(tmp_path)
    hits = {"count": 0}

    def client_factory(**kwargs):
        def handler(request: httpx.Request) -> httpx.Response:
            hits["count"] += 1
            return httpx.Response(
                403,
                headers={"content-type": "text/html"},
                text="<html>Access Denied</html>",
                request=request,
            )

        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    auth.httpx_client_factory = client_factory
    with pytest.raises(RuntimeError, match="403"):
        asyncio.run(auth.redirect_handler("https://api.ibkr.com/oauth2/authorize"))
    assert hits["count"] == 1


def test_unauthenticated_401_with_resource_metadata_is_not_an_access_block() -> None:
    result = classify_unauthenticated_mcp_response(
        url=_MCP_URL,
        status_code=401,
        headers={
            "content-type": "application/json",
            "www-authenticate": f'Bearer resource_metadata="{_PRM_URL}"',
        },
        body='{"error":"invalid_token"}',
        user_agent=honest_user_agent(),
    )
    assert result.access_block is False
    assert result.stopped is True
    assert result.resource_metadata_url == _PRM_URL
    assert result.status_code == 401
    assert "authorize" in result.note


def test_unauthenticated_403_html_is_an_access_block_and_stops() -> None:
    result = classify_unauthenticated_mcp_response(
        url=_MCP_URL,
        status_code=403,
        headers={"content-type": "text/html"},
        body="<html>Error 403 - Access Denied</html>",
        user_agent=honest_user_agent(),
    )
    assert result.access_block is True
    assert result.stopped is True
    assert result.resource_metadata_url is None
    assert "do not retry" in result.note.lower()
    assert looks_like_access_block(
        status_code=403,
        content_type="text/html",
        body="<html>Error 403 - Access Denied</html>",
        www_authenticate=None,
    )


def test_probe_issues_exactly_one_honest_get_and_stops_on_block() -> None:
    hits: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request)
        return httpx.Response(
            403,
            headers={"content-type": "text/html"},
            text="<html>Access Denied</html>",
            request=request,
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await probe_ibkr_mcp_public_unauthenticated(client)
            assert result.access_block is True
            assert result.stopped is True
            assert result.user_agent.startswith("Vibe-Trading/")

    asyncio.run(run())
    assert len(hits) == 1
    assert str(hits[0].url) == _MCP_URL
    ua = hits[0].headers.get("user-agent") or ""
    assert ua.startswith("Vibe-Trading/")
    assert all(marker not in ua for marker in BROWSER_IMPERSONATION_MARKERS)


def test_probe_does_not_follow_resource_metadata_on_401() -> None:
    """Minimal live diagnostic: a 401 challenge is recorded, then stop."""
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        if str(request.url) == _MCP_URL:
            return httpx.Response(
                401,
                headers={
                    "www-authenticate": f'Bearer resource_metadata="{_PRM_URL}"',
                    "content-type": "application/json",
                },
                json={"error": "invalid_token"},
                request=request,
            )
        return httpx.Response(500, text="must-not-follow", request=request)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await probe_ibkr_mcp_public_unauthenticated(client)
            assert result.access_block is False
            assert result.status_code == 401
            assert result.resource_metadata_url == _PRM_URL
            assert result.stopped is True

    asyncio.run(run())
    assert hits == [_MCP_URL]
