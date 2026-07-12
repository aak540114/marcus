"""
Unit tests for src/marcus_mcp/client.py — SimpleMarcusClient.

Regression coverage for a confirmed subprocess leak: initialize() calls
client_context.__aenter__() (spawning the MCP server subprocess) BEFORE
session.initialize() (the MCP handshake). If the handshake raised,
_initialized stayed False and the exception propagated straight out of
initialize() — the caller sees an exception, not a live client to call
close() on, so client_context.__aexit__() (the only place that tears the
subprocess down) was never reached. Every failed initialize() leaked one
orphaned child process.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.marcus_mcp.client import SimpleMarcusClient


@pytest.mark.asyncio
async def test_initialize_cleans_up_subprocess_on_handshake_failure():
    """A failing session.initialize() must still tear down the subprocess."""
    mock_context = MagicMock()
    mock_context.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
    mock_context.__aexit__ = AsyncMock(return_value=None)

    with patch("src.marcus_mcp.client.stdio_client", return_value=mock_context), patch(
        "src.marcus_mcp.client.ClientSession"
    ) as mock_session_cls:
        mock_session = MagicMock()
        mock_session.initialize = AsyncMock(side_effect=RuntimeError("handshake failed"))
        mock_session_cls.return_value = mock_session

        client = SimpleMarcusClient()

        with pytest.raises(RuntimeError, match="handshake failed"):
            await client.initialize()

        # The subprocess (opened by __aenter__) must have been torn down,
        # not leaked, even though initialize() raised.
        mock_context.__aexit__.assert_awaited_once_with(None, None, None)
        assert client.client_context is None
        assert client._initialized is False


@pytest.mark.asyncio
async def test_initialize_succeeds_and_does_not_call_aexit():
    """The happy path must not tear down the subprocess it just started."""
    mock_context = MagicMock()
    mock_context.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
    mock_context.__aexit__ = AsyncMock(return_value=None)

    with patch("src.marcus_mcp.client.stdio_client", return_value=mock_context), patch(
        "src.marcus_mcp.client.ClientSession"
    ) as mock_session_cls:
        mock_session = MagicMock()
        mock_session.initialize = AsyncMock(return_value=None)
        mock_session_cls.return_value = mock_session

        client = SimpleMarcusClient()
        await client.initialize()

        assert client._initialized is True
        assert client.client_context is mock_context
        mock_context.__aexit__.assert_not_awaited()


@pytest.mark.asyncio
async def test_initialize_is_idempotent_when_already_initialized():
    """A second initialize() call on an already-initialized client is a no-op."""
    mock_context = MagicMock()
    mock_context.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))

    with patch("src.marcus_mcp.client.stdio_client", return_value=mock_context), patch(
        "src.marcus_mcp.client.ClientSession"
    ) as mock_session_cls:
        mock_session = MagicMock()
        mock_session.initialize = AsyncMock(return_value=None)
        mock_session_cls.return_value = mock_session

        client = SimpleMarcusClient()
        await client.initialize()
        await client.initialize()  # second call

        mock_context.__aenter__.assert_awaited_once()  # not called again
