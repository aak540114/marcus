"""
Unit tests for src/marcus_mcp/audit.py — usage statistics from audit logs.

Regression coverage for a confirmed bug: get_usage_stats()'s per-line
try/except only caught json.JSONDecodeError, but event["timestamp"] and
event["event_type"] were accessed with plain [] indexing. A validly-parsed
JSON line missing either key raised an uncaught KeyError that propagated
out of get_usage_stats(), aborting the entire multi-file scan instead of
skipping just the one malformed record — contradicting the evident intent
of the try/except (tolerate corrupt log lines, not abort on them).
"""

import json

import pytest

from src.marcus_mcp.audit import AuditLogger


@pytest.fixture()
def logger(tmp_path):
    return AuditLogger(log_dir=tmp_path)


async def _write_log(tmp_path, filename, lines):
    (tmp_path / filename).write_text("\n".join(lines) + "\n")


class TestGetUsageStats:
    @pytest.mark.asyncio
    async def test_skips_malformed_json_line(self, logger, tmp_path):
        """Invalid JSON on one line doesn't abort the scan."""
        await _write_log(
            tmp_path,
            "audit_1.jsonl",
            [
                "NOT JSON {{{",
                json.dumps({"timestamp": "2024-01-01T00:00:00+00:00", "event_type": "tool_call"}),
            ],
        )
        stats = await logger.get_usage_stats()
        assert stats["total_events"] == 1

    @pytest.mark.asyncio
    async def test_skips_line_missing_timestamp(self, logger, tmp_path):
        """A valid JSON line missing 'timestamp' is skipped, not fatal."""
        await _write_log(
            tmp_path,
            "audit_1.jsonl",
            [
                json.dumps({"event_type": "tool_call"}),  # missing timestamp
                json.dumps({"timestamp": "2024-01-01T00:00:00+00:00", "event_type": "tool_call"}),
            ],
        )
        stats = await logger.get_usage_stats()
        assert stats["total_events"] == 1

    @pytest.mark.asyncio
    async def test_skips_line_missing_event_type(self, logger, tmp_path):
        """A valid JSON line missing 'event_type' is skipped, not fatal."""
        await _write_log(
            tmp_path,
            "audit_1.jsonl",
            [
                json.dumps({"timestamp": "2024-01-01T00:00:00+00:00"}),  # missing event_type
                json.dumps({"timestamp": "2024-01-01T00:00:00+00:00", "event_type": "tool_call"}),
            ],
        )
        stats = await logger.get_usage_stats()
        assert stats["total_events"] == 1

    @pytest.mark.asyncio
    async def test_skips_line_with_unparseable_timestamp(self, logger, tmp_path):
        """A present but unparseable timestamp string is skipped, not fatal."""
        await _write_log(
            tmp_path,
            "audit_1.jsonl",
            [
                json.dumps({"timestamp": "not-a-real-timestamp", "event_type": "tool_call"}),
                json.dumps({"timestamp": "2024-01-01T00:00:00+00:00", "event_type": "tool_call"}),
            ],
        )
        stats = await logger.get_usage_stats()
        assert stats["total_events"] == 1

    @pytest.mark.asyncio
    async def test_counts_by_event_type(self, logger, tmp_path):
        """Sanity check the happy path still aggregates correctly."""
        await _write_log(
            tmp_path,
            "audit_1.jsonl",
            [
                json.dumps({"timestamp": "2024-01-01T00:00:00+00:00", "event_type": "tool_call"}),
                json.dumps({"timestamp": "2024-01-01T00:01:00+00:00", "event_type": "tool_call"}),
                json.dumps({"timestamp": "2024-01-01T00:02:00+00:00", "event_type": "auth"}),
            ],
        )
        stats = await logger.get_usage_stats()
        assert stats["total_events"] == 3
        assert stats["by_event_type"] == {"tool_call": 2, "auth": 1}
