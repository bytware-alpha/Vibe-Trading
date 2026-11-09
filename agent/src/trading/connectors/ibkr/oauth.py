"""Honest public-client OAuth helpers for IBKR's official MCP endpoint.

This module is the IBKR-specific compatibility layer for standard MCP OAuth
(RFC 7591 public clients, RFC 8252 loopback redirects, RFC 8414 / RFC 9728
discovery). It does **not** impersonate a browser and it does not retry or
reroute around HTTP access blocks.

Protocol facts this layer is allowed to encode:

* IBKR's authorization server treats MCP connectors as public clients
  (``token_endpoint_auth_method: none``; no client secret).
* Dynamic registration binds a redirect URI, so the loopback callback port
  must be stable (``8765``) or a later run looks like a different client.
* Protected-resource metadata has advertised ``https://api.ibkr.com/oauth2``
  as an authorization server while the metadata document's ``issuer`` is
  ``https://api.ibkr.com``. mcp>=1.30 enforces RFC 8414 §3.3, so the
  path-suffixed identifier makes the client request
  ``/.well-known/oauth-authorization-server/oauth2`` (a 403 HTML page) and
  then reject the origin document. Normalizing that identifier onto the
  published issuer is discovery correctness, not a WAF bypass.
* FastMCP's stock authorization pre-flight rejects HTTP 301; IBKR's
  authorize endpoint has used 301. Accepting 301 is redirect correctness.

Not established here, and not claimed:

* That a Mozilla User-Agent or a spoofed ``Origin`` is required. Upstream
  #1186/#1190 sent those headers and captured them in a mock that still
  returned 403; that is not ablation evidence.
* That an unauthenticated HTTP 403 HTML block page from
  ``https://api.ibkr.com/v1/api/mcp-public`` is an OAuth protocol bug this
  client can repair. A 403 without a WWW-Authenticate challenge is treated
  as an access block: one request, then stop.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Mapping
from urllib.parse import parse_qs, urlparse

import httpx
from mcp.shared.auth import OAuthMetadata

IBKR_MCP_HOST = "api.ibkr.com"
IBKR_MCP_PUBLIC_URL = "https://api.ibkr.com/v1/api/mcp-public"
IBKR_ISSUER = "https://api.ibkr.com"
IBKR_AUTHORIZATION_ENDPOINT = "https://api.ibkr.com/oauth2/authorize"
IBKR_TOKEN_ENDPOINT = "https://api.ibkr.com/oauth2/api/v1/token"
IBKR_REGISTRATION_ENDPOINT = "https://api.ibkr.com/oauth2/register"
IBKR_OAUTH_CALLBACK_PORT = 8765
IBKR_OAUTH_ACCEPT = "application/json"
IBKR_READONLY_OAUTH_SCOPE = "mcp.read"
# Write/order scopes IBKR advertises on protected-resource metadata. A
# read-only client must never send these on registration or the authorize URL,
# even when ``scopes_supported`` or WWW-Authenticate lists them.
IBKR_WRITE_OAUTH_SCOPES: frozenset[str] = frozenset(
    {
        "mcp.write",
        "mcp.orders.submit",
        "mcp.orders.place",
        "mcp.orders.cancel",
        "mcp.orders.modify",
    }
)
# Extra identity scopes the wildcard probe already permits in config.
IBKR_READONLY_OAUTH_EXTRA_SCOPES: frozenset[str] = frozenset(
    {"openid", "profile", "email", "account-ids"}
)
IBKR_READONLY_OAUTH_ALLOWED_SCOPES: frozenset[str] = frozenset(
    {IBKR_READONLY_OAUTH_SCOPE} | IBKR_READONLY_OAUTH_EXTRA_SCOPES
)
# Documented IBKR official MCP scopes for an explicit trading-capability
# connection. Requested only when the operator seed lists them; never by
# unioning ``scopes_supported`` onto a mcp.read seed.
IBKR_TRADING_OAUTH_SCOPES: tuple[str, ...] = (
    IBKR_READONLY_OAUTH_SCOPE,
    "mcp.write",
    "mcp.orders.submit",
)
IBKR_TRADING_OAUTH_SCOPE_SET: frozenset[str] = frozenset(IBKR_TRADING_OAUTH_SCOPES)
_DISTRIBUTION_NAME = "vibe-trading-ai"


class IBKROAuthScopeError(RuntimeError):
    """Configured IBKR OAuth scopes cannot be enforced without metadata widening."""

# Substrings that identify browser impersonation. An honest client identity
# such as ``Vibe-Trading/0.1.19`` is required; these are not.
BROWSER_IMPERSONATION_MARKERS: tuple[str, ...] = (
    "Mozilla/",
    "AppleWebKit/",
    "Chrome/",
    "Safari/",
    "Gecko/",
    "Edg/",
    "Firefox/",
)


def honest_user_agent() -> str:
    """Return the product User-Agent this client actually is.

    Returns:
        ``Vibe-Trading/<installed version>``, or ``Vibe-Trading/dev`` when the
        distribution metadata is missing (an uninstalled source checkout).
    """
    try:
        package_version = version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        package_version = "dev"
    return f"Vibe-Trading/{package_version}"


def public_client_registration_metadata() -> dict[str, str]:
    """RFC 7591 metadata for a public (no-secret) native MCP client."""
    return {"token_endpoint_auth_method": "none"}


def parse_oauth_scope_tokens(value: object) -> tuple[str, ...]:
    """Split an OAuth scope list or encoded string into tokens."""
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        tokens: list[str] = []
        for item in value:
            tokens.extend(parse_oauth_scope_tokens(item))
        return tuple(tokens)
    text = str(value).replace(",", " ").replace("+", " ")
    return tuple(token for token in text.split() if token)


def is_ibkr_write_oauth_scope(scope: str) -> bool:
    """Return whether ``scope`` is an IBKR write or order-submit grant."""
    token = scope.strip().lower()
    if token in IBKR_WRITE_OAUTH_SCOPES:
        return True
    return token.startswith("mcp.write") or token.startswith("mcp.orders.")


def require_ibkr_readonly_oauth_scopes(configured: Sequence[str] | object) -> str:
    """Return the space-separated scope string this client is allowed to request.

    Metadata ``scopes_supported`` / WWW-Authenticate ``scope`` must never be
    used as the requested set. Empty config would otherwise inherit every
    advertised write/order scope; that is fail-closed here.

    Args:
        configured: Operator-configured OAuth scopes (list or encoded string).

    Returns:
        Space-separated read-only scopes, preserving configured order.

    Raises:
        IBKROAuthScopeError: If the configured set is empty, missing
            ``mcp.read``, or includes a write/order scope.
    """
    tokens = parse_oauth_scope_tokens(configured)
    if not tokens:
        raise IBKROAuthScopeError(
            "IBKR official MCP OAuth has no configured scopes; refusing to "
            "default to authorization-server scopes_supported (which includes "
            "mcp.write / mcp.orders.submit)"
        )
    unique = {token for token in tokens}
    write = {token for token in unique if is_ibkr_write_oauth_scope(token)}
    if write:
        raise IBKROAuthScopeError(
            "IBKR official MCP OAuth refuses write/order scopes "
            f"{sorted(write)}; configured scopes must be mcp.read-only"
        )
    if IBKR_READONLY_OAUTH_SCOPE not in unique:
        raise IBKROAuthScopeError(
            "IBKR official MCP OAuth requires mcp.read and cannot enforce a "
            "read-only grant from the configured scopes"
        )
    extras = unique - IBKR_READONLY_OAUTH_ALLOWED_SCOPES
    if extras:
        raise IBKROAuthScopeError(
            "IBKR official MCP OAuth refuses undeclared scopes "
            f"{sorted(extras)}; read-only grant is mcp.read only"
        )
    ordered: list[str] = []
    for token in tokens:
        if token not in ordered:
            ordered.append(token)
    return " ".join(ordered)


def require_ibkr_configured_oauth_scopes(configured: Sequence[str] | object) -> str:
    """Return the scope string for the configured IBKR grant, never widened.

    A mcp.read-only seed stays mcp.read-only. An explicit trading-capability
    seed may request the documented ``mcp.read mcp.write mcp.orders.submit``
    set. Empty config or any other write/order mix fails closed.

    Args:
        configured: Operator-configured OAuth scopes.

    Returns:
        Space-separated scopes to send on registration and authorize.

    Raises:
        IBKROAuthScopeError: If the configured set cannot be enforced as-is.
    """
    tokens = parse_oauth_scope_tokens(configured)
    unique = {token for token in tokens}
    write = {token for token in unique if is_ibkr_write_oauth_scope(token)}
    if not write:
        return require_ibkr_readonly_oauth_scopes(configured)
    if IBKR_READONLY_OAUTH_SCOPE not in unique:
        raise IBKROAuthScopeError(
            "IBKR trading-capability OAuth requires mcp.read in addition to "
            "the documented write/order scopes"
        )
    missing = IBKR_TRADING_OAUTH_SCOPE_SET - unique
    if missing:
        raise IBKROAuthScopeError(
            "IBKR trading-capability OAuth requires documented scopes "
            f"{list(IBKR_TRADING_OAUTH_SCOPES)}; missing {sorted(missing)}"
        )
    extra_write = write - IBKR_TRADING_OAUTH_SCOPE_SET
    if extra_write:
        raise IBKROAuthScopeError(
            "IBKR official MCP OAuth refuses undeclared write/order scopes "
            f"{sorted(extra_write)}"
        )
    extras = unique - IBKR_TRADING_OAUTH_SCOPE_SET - IBKR_READONLY_OAUTH_EXTRA_SCOPES
    if extras:
        raise IBKROAuthScopeError(
            "IBKR official MCP OAuth refuses undeclared scopes "
            f"{sorted(extras)}"
        )
    ordered = [scope for scope in IBKR_TRADING_OAUTH_SCOPES if scope in unique]
    for token in tokens:
        if token not in ordered:
            ordered.append(token)
    return " ".join(ordered)


def pin_configured_ibkr_oauth_scopes(
    *,
    configured: Sequence[str] | object,
    metadata_scope: str | None,
) -> str:
    """Choose authorize/register scopes without widening from metadata.

    Args:
        configured: Operator-configured scopes (the only allowed request set).
        metadata_scope: ``scopes_supported`` or WWW-Authenticate ``scope``.
            Advertised extras are ignored; they must not be unioned in.

    Returns:
        The configured scope string (readonly or explicit trading-capability).

    Raises:
        IBKROAuthScopeError: If the configured grant cannot be enforced.
    """
    # ``metadata_scope`` is accepted so callers pass PRM/WWW-Authenticate
    # values explicitly, then discarded: it must never be unioned in.
    del metadata_scope
    return require_ibkr_configured_oauth_scopes(configured)


def authorization_url_scope_tokens(authorization_url: str) -> tuple[str, ...]:
    """Return scope tokens from an OAuth authorize URL query string."""
    query = parse_qs(urlparse(authorization_url).query)
    raw_values = query.get("scope") or []
    tokens: list[str] = []
    for raw in raw_values:
        tokens.extend(parse_oauth_scope_tokens(raw))
    return tuple(tokens)


def assert_ibkr_authorization_url_is_readonly(
    authorization_url: str,
    *,
    configured: Sequence[str] | object,
) -> None:
    """Refuse to open a browser when the authorize URL is not mcp.read-only.

    Args:
        authorization_url: URL FastMCP is about to pre-flight and open.
        configured: Operator-configured scopes that must appear unchanged.

    Raises:
        IBKROAuthScopeError: If the URL omits ``mcp.read`` or includes any
            write/order (or other non-configured) scope.
    """
    expected = set(parse_oauth_scope_tokens(require_ibkr_readonly_oauth_scopes(configured)))
    actual = set(authorization_url_scope_tokens(authorization_url))
    extra = actual - expected
    if extra:
        raise IBKROAuthScopeError(
            "Refusing to open IBKR authorize URL: scope was widened beyond "
            f"configured mcp.read-only to include {sorted(extra)}"
        )
    missing = expected - actual
    if missing:
        raise IBKROAuthScopeError(
            "Refusing to open IBKR authorize URL: missing required scope "
            f"{sorted(missing)}"
        )


def assert_ibkr_authorization_url_matches_configured(
    authorization_url: str,
    *,
    configured: Sequence[str] | object,
) -> None:
    """Refuse to open a browser when authorize scopes differ from config.

    Readonly seeds must stay mcp.read-only. Trading-capability seeds must
    send exactly the documented configured set, with no metadata extras.

    Args:
        authorization_url: URL FastMCP is about to pre-flight and open.
        configured: Operator-configured scopes that must appear unchanged.

    Raises:
        IBKROAuthScopeError: If the URL was widened or is missing a
            configured scope.
    """
    expected = set(parse_oauth_scope_tokens(require_ibkr_configured_oauth_scopes(configured)))
    actual = set(authorization_url_scope_tokens(authorization_url))
    extra = actual - expected
    if extra:
        raise IBKROAuthScopeError(
            "Refusing to open IBKR authorize URL: scope was widened beyond "
            f"configured scopes to include {sorted(extra)}"
        )
    missing = expected - actual
    if missing:
        raise IBKROAuthScopeError(
            "Refusing to open IBKR authorize URL: missing required scope "
            f"{sorted(missing)}"
        )


def assert_ibkr_readonly_remote_tool(
    remote_name: str,
    *,
    order_trading: bool = False,
    granted_scopes: Sequence[str] | object = (),
) -> None:
    """Refuse IBKR MCP tool calls that are not vetted reads or an armed order.

    Live-order tools stay off unless ``order_trading`` is true and
    ``granted_scopes`` includes ``mcp.orders.submit``. Requested scopes are
    not enough: the cached grant is what counts. Alerts, watchlists,
    screeners, instructions, feedback, and ``modify_order`` stay blocked.

    Args:
        remote_name: Remote IBKR MCP tool name.
        order_trading: Local ``orderTrading`` flag. Default false.
        granted_scopes: Scopes on the cached token.

    Raises:
        IBKROAuthScopeError: If ``remote_name`` is not allowed.
    """
    from src.trading.connectors.ibkr.order_gate import assert_ibkr_remote_tool

    assert_ibkr_remote_tool(
        remote_name, order_trading=order_trading, granted_scopes=granted_scopes
    )


def is_ibkr_mcp_host(hostname: str | None) -> bool:
    """Return whether ``hostname`` is IBKR's official MCP/API host."""
    return (hostname or "").lower() == IBKR_MCP_HOST


def is_ibkr_mcp_resource_path(path: str) -> bool:
    """Return whether ``path`` is an IBKR MCP resource (not an OAuth endpoint)."""
    return path.startswith("/v1/api/mcp")


def normalize_ibkr_authorization_server(url: str) -> str:
    """Map IBKR's path-suffixed AS identifier onto the issuer it publishes.

    Args:
        url: Authorization-server identifier from protected-resource metadata.

    Returns:
        ``https://api.ibkr.com`` when ``url`` is IBKR's ``/oauth2`` alias or the
        origin itself; otherwise ``url`` unchanged.
    """
    parsed = urlparse(url.strip())
    if not is_ibkr_mcp_host(parsed.hostname):
        return url
    path = (parsed.path or "").rstrip("/")
    if path in ("", "/oauth2"):
        return f"{parsed.scheme}://{parsed.hostname}"
    return url


def pinned_ibkr_oauth_metadata() -> OAuthMetadata:
    """Issuer-aligned IBKR authorization-server metadata.

    Used when refresh runs without a prior discovery document. Endpoints match
    IBKR's published AS metadata (authorization ``/oauth2/authorize``, token
    ``/oauth2/api/v1/token``, registration ``/oauth2/register``).
    """
    return OAuthMetadata(
        issuer=IBKR_ISSUER,
        authorization_endpoint=IBKR_AUTHORIZATION_ENDPOINT,
        token_endpoint=IBKR_TOKEN_ENDPOINT,
        registration_endpoint=IBKR_REGISTRATION_ENDPOINT,
    )


def apply_honest_ibkr_client_headers(request: httpx.Request) -> httpx.Request:
    """Identify this client honestly on an IBKR request.

    Sets ``User-Agent`` to :func:`honest_user_agent`. Strips a spoofed
    ``Origin`` if one is present. Uses JSON ``Accept`` on OAuth/discovery
    endpoints while leaving the MCP resource ``Accept`` (JSON + event-stream)
    alone.

    Args:
        request: Outgoing httpx request, mutated in place.

    Returns:
        The same request object.
    """
    request.headers["User-Agent"] = honest_user_agent()
    if "origin" in request.headers:
        del request.headers["origin"]
    if not is_ibkr_mcp_resource_path(request.url.path):
        request.headers["Accept"] = IBKR_OAUTH_ACCEPT
    return request


def looks_like_access_block(
    *,
    status_code: int,
    content_type: str,
    body: str,
    www_authenticate: str | None,
) -> bool:
    """Return whether an unauthenticated response is an access block.

    A 401/403 that carries a WWW-Authenticate challenge is MCP OAuth, not a
    block. A 403 without that challenge — HTML or an "Access Denied" body —
    is treated as an access block. Callers must not retry, impersonate, or
    re-target the URL.
    """
    if www_authenticate:
        return False
    if status_code != 403:
        return False
    lowered_type = content_type.lower()
    snippet = body[:1024].lower()
    if "text/html" in lowered_type or "application/xhtml" in lowered_type:
        return True
    return (
        "<html" in snippet
        or "<!doctype html" in snippet
        or "access denied" in snippet
        or "error 403" in snippet
        or not body.strip()
    )


@dataclass(frozen=True)
class UnauthenticatedProbeResult:
    """Outcome of a single unauthenticated GET to IBKR's public MCP URL.

    Attributes:
        url: Requested URL.
        status_code: HTTP status of that one request.
        content_type: Response Content-Type, possibly empty.
        www_authenticate: WWW-Authenticate header if present.
        resource_metadata_url: ``resource_metadata`` from the challenge, if any.
        access_block: True when the response is an access block page.
        stopped: Always True after one request; included so callers cannot
            mistake this for a multi-step discovery run.
        user_agent: User-Agent that was sent.
        note: Operator-facing explanation. Does not claim live OAuth success.
    """

    url: str
    status_code: int
    content_type: str
    www_authenticate: str | None
    resource_metadata_url: str | None
    access_block: bool
    stopped: bool
    user_agent: str
    note: str


def classify_unauthenticated_mcp_response(
    *,
    url: str,
    status_code: int,
    headers: Mapping[str, str],
    body: str,
    user_agent: str,
) -> UnauthenticatedProbeResult:
    """Classify one unauthenticated MCP GET. Never issues a follow-up request."""
    content_type = headers.get("content-type") or headers.get("Content-Type") or ""
    www_authenticate = headers.get("www-authenticate") or headers.get("WWW-Authenticate")
    resource_metadata_url = _resource_metadata_from_www_authenticate(www_authenticate)
    access_block = looks_like_access_block(
        status_code=status_code,
        content_type=content_type,
        body=body,
        www_authenticate=www_authenticate,
    )
    if access_block:
        note = (
            "HTTP 403 access block on the public MCP URL. This is not classified "
            "as an OAuth protocol failure. Do not retry, impersonate a browser, "
            "or re-target the endpoint. Owner OAuth cannot start until IBKR "
            "returns an MCP challenge (typically 401 with WWW-Authenticate)."
        )
    elif status_code == 401 and resource_metadata_url:
        note = (
            "Unauthenticated MCP challenge as expected (HTTP 401 with "
            "resource_metadata). This probe stops here; completing OAuth "
            "requires the account owner to run "
            "`vibe-trading connector authorize ibkr-live-official-mcp-readonly` "
            "on their machine. Mock-server protocol tests are not live success."
        )
    elif status_code == 401:
        note = (
            "HTTP 401 without a parseable resource_metadata parameter. The "
            "endpoint exists and requires auth; discovery metadata was not "
            "advertised on this response. Probe stops; no retry."
        )
    else:
        note = (
            f"Unauthenticated GET returned HTTP {status_code}. Probe stops after "
            "this single request; no follow-up discovery or registration."
        )
    return UnauthenticatedProbeResult(
        url=url,
        status_code=status_code,
        content_type=content_type,
        www_authenticate=www_authenticate,
        resource_metadata_url=resource_metadata_url,
        access_block=access_block,
        stopped=True,
        user_agent=user_agent,
        note=note,
    )


def _resource_metadata_from_www_authenticate(header: str | None) -> str | None:
    """Extract RFC 9728 ``resource_metadata`` from a WWW-Authenticate header."""
    if not header:
        return None
    marker = "resource_metadata="
    lowered = header
    index = lowered.lower().find(marker)
    if index < 0:
        return None
    rest = header[index + len(marker) :]
    if rest.startswith('"'):
        end = rest.find('"', 1)
        return rest[1:end] if end > 0 else None
    token = rest.split(",", 1)[0].strip()
    return token or None


async def probe_ibkr_mcp_public_unauthenticated(
    client: httpx.AsyncClient,
    *,
    url: str = IBKR_MCP_PUBLIC_URL,
) -> UnauthenticatedProbeResult:
    """Send one honest unauthenticated GET and stop.

    Args:
        client: Injected httpx client (mock in tests; a real client only for an
            operator-run diagnostic).
        url: MCP URL to probe. Defaults to IBKR's published public endpoint.

    Returns:
        Classification of that single response. Never retries.
    """
    user_agent = honest_user_agent()
    response = await client.get(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json, text/event-stream",
        },
    )
    return classify_unauthenticated_mcp_response(
        url=str(response.request.url),
        status_code=response.status_code,
        headers=response.headers,
        body=response.text,
        user_agent=user_agent,
    )


def authorization_preflight_accepted(status_code: int) -> bool:
    """Return whether an authorization-endpoint pre-flight status is usable.

    FastMCP's stock handler accepts 200/302/303/307/308 and rejects 301.
    IBKR has answered the authorize URL with 301; that is still a redirect
    to a login page, not a protocol error.
    """
    return status_code in {200, 301, 302, 303, 307, 308}
