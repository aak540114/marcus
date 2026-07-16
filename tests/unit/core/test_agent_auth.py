"""
Unit tests for the bearer-token authentication middleware.

Verifies that Marcus's HTTP surface rejects requests without a valid
``Authorization: Bearer <token>`` header when a token is configured, stays
open when no token is set (localhost default), and never gates exempt
paths (the Kanboard webhook, which authenticates by its own ``?token=``).
"""

import os
from typing import Any, Dict, List, Tuple
from unittest.mock import patch

import pytest

from src.core.agent_auth import BearerAuthMiddleware, get_agent_token

pytestmark = pytest.mark.unit


def _http_scope(path: str = "/mcp", auth: str | None = None) -> Dict[str, Any]:
    """Build a minimal ASGI HTTP scope, optionally with an auth header."""
    headers: List[Tuple[bytes, bytes]] = []
    if auth is not None:
        headers.append((b"authorization", auth.encode("latin-1")))
    return {"type": "http", "path": path, "headers": headers}


class _Recorder:
    """A stand-in downstream ASGI app that records whether it was called."""

    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.called = True


async def _drain_send() -> Tuple[List[Dict[str, Any]], Any]:
    """Return a (messages, send) pair capturing what the middleware sends."""
    messages: List[Dict[str, Any]] = []

    async def send(message: Dict[str, Any]) -> None:
        messages.append(message)

    return messages, send


async def _noop_receive() -> Dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


class TestGetAgentToken:
    """get_agent_token() reads MARCUS_AGENT_TOKEN, treating blank as unset."""

    def test_returns_none_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert get_agent_token() is None

    def test_returns_none_when_blank(self) -> None:
        with patch.dict(os.environ, {"MARCUS_AGENT_TOKEN": "   "}, clear=True):
            assert get_agent_token() is None

    def test_returns_stripped_token(self) -> None:
        with patch.dict(os.environ, {"MARCUS_AGENT_TOKEN": "  secret  "}, clear=True):
            assert get_agent_token() == "secret"


@pytest.mark.asyncio
class TestBearerAuthMiddleware:
    """Enforcement behavior of the bearer-token middleware."""

    async def test_passthrough_when_no_token_configured(self) -> None:
        """Auth disabled (token=None) → every request passes through."""
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token=None)
        messages, send = await _drain_send()

        await mw(_http_scope(auth=None), _noop_receive, send)

        assert inner.called is True
        assert messages == []  # middleware sent nothing itself

    async def test_rejects_request_with_no_auth_header(self) -> None:
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="secret")
        messages, send = await _drain_send()

        await mw(_http_scope(auth=None), _noop_receive, send)

        assert inner.called is False
        assert messages[0]["status"] == 401

    async def test_rejects_wrong_token(self) -> None:
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="secret")
        messages, send = await _drain_send()

        await mw(_http_scope(auth="Bearer wrong"), _noop_receive, send)

        assert inner.called is False
        assert messages[0]["status"] == 401

    async def test_rejects_non_bearer_scheme(self) -> None:
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="secret")
        messages, send = await _drain_send()

        await mw(_http_scope(auth="Basic secret"), _noop_receive, send)

        assert inner.called is False
        assert messages[0]["status"] == 401

    async def test_allows_correct_token(self) -> None:
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="secret")
        messages, send = await _drain_send()

        await mw(_http_scope(auth="Bearer secret"), _noop_receive, send)

        assert inner.called is True
        assert messages == []

    async def test_bearer_scheme_is_case_insensitive(self) -> None:
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="secret")
        messages, send = await _drain_send()

        await mw(_http_scope(auth="bearer secret"), _noop_receive, send)

        assert inner.called is True

    async def test_non_ascii_token_rejected_without_crashing(self) -> None:
        """A hostile non-ASCII bearer value must cleanly 401, not raise a
        TypeError from secrets.compare_digest (which would surface as a 500).
        """
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="secret")
        messages, send = await _drain_send()

        # 0xFF decodes (latin-1) to U+00FF — non-ASCII — inside the middleware.
        await mw(_http_scope(auth="Bearer ÿÿ"), _noop_receive, send)

        assert inner.called is False
        assert messages[0]["status"] == 401

    async def test_webhook_path_is_exempt(self) -> None:
        """The Kanboard webhook authenticates by its own ?token=, so the
        bearer middleware must let it through even with no header."""
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="secret")
        messages, send = await _drain_send()

        await mw(
            _http_scope(path="/webhooks/kanboard", auth=None), _noop_receive, send
        )

        assert inner.called is True
        assert messages == []

    async def test_gitea_webhook_path_is_exempt(self) -> None:
        """The Gitea webhook authenticates via its own HMAC signature, so
        the bearer middleware must let it through even with no header."""
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="secret")
        messages, send = await _drain_send()

        await mw(
            _http_scope(path="/webhooks/gitea", auth=None), _noop_receive, send
        )

        assert inner.called is True
        assert messages == []

    async def test_non_http_scope_passes_through(self) -> None:
        """lifespan/websocket scopes are not gated."""
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="secret")
        messages, send = await _drain_send()

        await mw({"type": "lifespan"}, _noop_receive, send)

        assert inner.called is True

    async def test_protected_api_route_requires_token(self) -> None:
        """A state-mutating API route (gate flip) must be gated too, not
        just the MCP mount."""
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="secret")
        messages, send = await _drain_send()

        await mw(
            _http_scope(path="/api/gate-setting/project", auth=None),
            _noop_receive,
            send,
        )

        assert inner.called is False
        assert messages[0]["status"] == 401


def _http_scope_with_query(
    path: str, query: str, auth: str | None = None
) -> Dict[str, Any]:
    """Build an ASGI HTTP scope carrying a raw query string."""
    scope = _http_scope(path=path, auth=auth)
    scope["query_string"] = query.encode("latin-1")
    return scope


@pytest.mark.asyncio
class TestQueryParamToken:
    """The Kanboard plugin's browser JS and plain navigation links cannot
    attach an Authorization header, so the middleware must also accept the
    shared secret as a ``?token=`` query parameter (mirroring how Kanboard's
    own webhook authenticates)."""

    async def test_correct_query_token_passes(self) -> None:
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="secret")
        messages, send = await _drain_send()

        await mw(
            _http_scope_with_query("/api/active-agents", "token=secret"),
            _noop_receive,
            send,
        )

        assert inner.called is True
        assert messages == []

    async def test_wrong_query_token_rejected(self) -> None:
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="secret")
        messages, send = await _drain_send()

        await mw(
            _http_scope_with_query("/api/active-agents", "token=wrong"),
            _noop_receive,
            send,
        )

        assert inner.called is False
        assert messages[0]["status"] == 401

    async def test_query_token_amid_other_params(self) -> None:
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="secret")
        messages, send = await _drain_send()

        await mw(
            _http_scope_with_query(
                "/dev-env/view", "ticket_id=7&token=secret&x=1"
            ),
            _noop_receive,
            send,
        )

        assert inner.called is True

    async def test_valid_header_wins_over_wrong_query_token(self) -> None:
        """A correct Authorization header must authenticate even if a stale
        or wrong ?token= also appears in the URL."""
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="secret")
        messages, send = await _drain_send()

        await mw(
            _http_scope_with_query(
                "/api/active-agents", "token=stale", auth="Bearer secret"
            ),
            _noop_receive,
            send,
        )

        assert inner.called is True

    async def test_url_encoded_query_token_passes(self) -> None:
        """Percent-encoded token values must be decoded before comparison
        (browsers encode special characters in query strings)."""
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="se+cret")
        messages, send = await _drain_send()

        await mw(
            _http_scope_with_query("/api/active-agents", "token=se%2Bcret"),
            _noop_receive,
            send,
        )

        assert inner.called is True

    async def test_malformed_query_string_rejected_without_crashing(self) -> None:
        """A hostile/binary query string must cleanly 401, never 500."""
        inner = _Recorder()
        mw = BearerAuthMiddleware(inner, token="secret")
        messages, send = await _drain_send()

        scope = _http_scope("/api/active-agents")
        scope["query_string"] = b"\xff\xfe=\xff&token"
        await mw(scope, _noop_receive, send)

        assert inner.called is False
        assert messages[0]["status"] == 401
