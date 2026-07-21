"""
Unit tests for the ``/dev-env/view`` interim "building preview" page.

Background: Marcus can start a per-ticket Docker container that serves a
ticket's branch so a human can open a browser and *see* what the AI agents
built. That container starts asynchronously — ``docker run -d`` returns
before the app inside is actually listening — so the view route serves a
lightweight HTML page that polls a status endpoint and only redirects the
browser once the app is truly up. These tests verify that page's contract.
"""

import json

from src.marcus_mcp.server import _dev_env_starting_page


class TestDevEnvStartingPage:
    """The interim page must poll status and redirect only when serving."""

    def test_polls_the_status_endpoint(self) -> None:
        """The page fetches /api/dev-env/status to learn when to redirect."""
        page = _dev_env_starting_page("7", "kanboard", "http://localhost:9123")
        assert "/api/dev-env/status" in page

    def test_redirects_only_on_serving(self) -> None:
        """The client-side script keys the redirect off the `serving` flag."""
        page = _dev_env_starting_page("7", "kanboard", "http://localhost:9123")
        assert "s.serving" in page
        assert "window.location" in page

    def test_embeds_ticket_and_url_as_json(self) -> None:
        """Ticket id, provider, and URL are embedded as safe JS literals."""
        page = _dev_env_starting_page("7", "kanboard", "http://localhost:9123")
        assert json.dumps("7") in page
        assert json.dumps("kanboard") in page
        assert json.dumps("http://localhost:9123") in page

    def test_is_a_complete_html_document(self) -> None:
        """The helper returns a full standalone HTML page."""
        page = _dev_env_starting_page("7", "kanboard", "http://localhost:9123")
        assert page.lstrip().startswith("<!doctype html>")
        assert "</html>" in page

    def test_escapes_ticket_id_in_markup(self) -> None:
        """A ticket id is HTML-escaped where it appears as page text."""
        # The route validates ids to [A-Za-z0-9._-], but the template must
        # still be robust: a '<' must never render as raw markup.
        page = _dev_env_starting_page("a<b", "kanboard", "http://localhost:9123")
        assert "a<b</h1>" not in page
        assert "a&lt;b" in page

    def test_has_wall_clock_timeout_backstop(self) -> None:
        """The poller gives up after a bounded wall-clock wait so it can never
        spin forever (container died before first poll, or unobservable port)."""
        page = _dev_env_starting_page("7", "kanboard", "http://localhost:9123")
        assert "MAX_WAIT_MS" in page
        # On timeout it offers a direct link to the preview URL, not only retry.
        assert "Open preview" in page
