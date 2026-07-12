"""
Unit tests for src/marcus_mcp/client_manager.py — session expiry logic.

Regression coverage for a confirmed bug: ClientManager._is_expired() (used
by the 5-minute cleanup loop) previously compared timedelta.seconds instead
of timedelta.total_seconds(). .seconds is only the sub-day remainder of a
timedelta, so a session idle for 1 day + 30 minutes had .seconds == 1800 —
under the 3600s threshold — and was never cleaned up, leaking ClientSession
entries (and their allowed_tools) indefinitely for any session that crossed
a day boundary at the wrong offset.
"""

from datetime import datetime, timedelta, timezone

from src.marcus_mcp.client_manager import ClientManager, ClientSession


def _session_idle_for(delta: timedelta) -> ClientSession:
    """Build a session whose last_activity is `delta` in the past."""
    session = ClientSession(session_id="s1")
    session.last_activity = datetime.now(timezone.utc) - delta
    return session


class TestIsExpired:
    """ClientManager._is_expired() — the core expiry comparison."""

    def test_session_idle_under_one_hour_is_not_expired(self):
        session = _session_idle_for(timedelta(minutes=30))
        now = datetime.now(timezone.utc)
        assert ClientManager._is_expired(session, now) is False

    def test_session_idle_over_one_hour_is_expired(self):
        session = _session_idle_for(timedelta(hours=2))
        now = datetime.now(timezone.utc)
        assert ClientManager._is_expired(session, now) is True

    def test_session_idle_one_day_and_thirty_minutes_is_expired(self):
        """Regression case: timedelta.seconds alone would say 1800s (not
        expired); the real elapsed time is ~24.5 hours (expired)."""
        session = _session_idle_for(timedelta(days=1, minutes=30))
        now = datetime.now(timezone.utc)

        # Confirm this is exactly the case where .seconds and total_seconds()
        # disagree — otherwise this test wouldn't actually catch a regression.
        delta = now - session.last_activity
        assert delta.seconds <= 3600  # the buggy comparison would say "fresh"
        assert delta.total_seconds() > 3600  # the real elapsed time

        assert ClientManager._is_expired(session, now) is True

    def test_custom_timeout_is_respected(self):
        session = _session_idle_for(timedelta(minutes=10))
        now = datetime.now(timezone.utc)
        assert ClientManager._is_expired(session, now, timeout_seconds=300) is True
        assert ClientManager._is_expired(session, now, timeout_seconds=900) is False
