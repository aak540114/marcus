"""
Unit tests for scripts/provision_kanboard.py

All Kanboard JSON-RPC calls go through ``urllib.request.urlopen`` — every
test mocks that single seam, no real HTTP or Kanboard instance involved.
"""

import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "scripts"))

import provision_kanboard as pk  # noqa: E402


def _rpc_response(result) -> MagicMock:
    """Build a mock urlopen() context manager returning a JSON-RPC result."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()
    resp = MagicMock()
    resp.read = MagicMock(return_value=body)
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _rpc_error(message: str) -> MagicMock:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": message}}
    ).encode()
    resp = MagicMock()
    resp.read = MagicMock(return_value=body)
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestCallRpc:
    """call_rpc() — the single HTTP seam used by everything else."""

    def test_returns_result_on_success(self):
        with patch("provision_kanboard.urllib.request.urlopen", return_value=_rpc_response(42)):
            result = pk.call_rpc("http://x/jsonrpc.php", "tok", "getVersion")
        assert result == 42

    def test_sends_basic_auth_as_jsonrpc_user(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["auth"] = req.get_header("Authorization")
            return _rpc_response("ok")

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=fake_urlopen):
            pk.call_rpc("http://x/jsonrpc.php", "sekret", "getVersion")

        import base64

        expected = "Basic " + base64.b64encode(b"jsonrpc:sekret").decode()
        assert captured["auth"] == expected

    def test_raises_auth_error_on_401_without_retry(self):
        call_count = {"n": 0}

        def fake_urlopen(req, timeout=None):
            call_count["n"] += 1
            raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=fake_urlopen):
            with pytest.raises(pk.KanboardAuthError):
                pk.call_rpc("http://x/jsonrpc.php", "bad-tok", "getVersion", retries=5)

        assert call_count["n"] == 1  # no retry on auth failure

    def test_raises_auth_error_on_403(self):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, None)

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=fake_urlopen):
            with pytest.raises(pk.KanboardAuthError):
                pk.call_rpc("http://x/jsonrpc.php", "bad-tok", "getVersion")

    def test_retries_connection_error_then_succeeds(self):
        responses = iter(
            [
                urllib.error.URLError("connection refused"),
                urllib.error.URLError("connection refused"),
                _rpc_response("ok"),
            ]
        )

        def fake_urlopen(req, timeout=None):
            r = next(responses)
            if isinstance(r, Exception):
                raise r
            return r

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=fake_urlopen), patch(
            "provision_kanboard.time.sleep"
        ):
            result = pk.call_rpc("http://x/jsonrpc.php", "tok", "getVersion", retries=5, retry_delay=0)

        assert result == "ok"

    def test_gives_up_after_max_retries(self):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=fake_urlopen), patch(
            "provision_kanboard.time.sleep"
        ):
            with pytest.raises(pk.KanboardRPCError):
                pk.call_rpc("http://x/jsonrpc.php", "tok", "getVersion", retries=3, retry_delay=0)

    def test_raises_rpc_error_on_error_field(self):
        with patch(
            "provision_kanboard.urllib.request.urlopen", return_value=_rpc_error("Invalid params")
        ):
            with pytest.raises(pk.KanboardRPCError):
                pk.call_rpc("http://x/jsonrpc.php", "tok", "createProject")


class TestFindOrCreateProject:
    def test_returns_existing_project_id_without_creating(self):
        responses = [_rpc_response({"id": "7", "name": "Marcus Project"})]

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=responses) as m:
            project_id = pk.find_or_create_project("http://x/jsonrpc.php", "tok", "Marcus Project")

        assert project_id == 7
        assert m.call_count == 1  # only getProjectByName — no createProject call

    def test_creates_project_when_missing(self):
        responses = [
            _rpc_response(False),  # getProjectByName: not found
            _rpc_response(True),  # createProject
            _rpc_response({"id": "3", "name": "Marcus Project"}),  # getProjectByName again
        ]

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=responses):
            project_id = pk.find_or_create_project("http://x/jsonrpc.php", "tok", "Marcus Project")

        assert project_id == 3

    def test_raises_if_create_did_not_take(self):
        responses = [
            _rpc_response(False),
            _rpc_response(True),
            _rpc_response(False),  # still not found after creation attempt
        ]

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=responses):
            with pytest.raises(pk.KanboardRPCError):
                pk.find_or_create_project("http://x/jsonrpc.php", "tok", "Marcus Project")


class TestReconcileColumns:
    def test_renames_default_columns_and_adds_missing(self):
        columns = [
            {"id": "1", "title": "Backlog"},
            {"id": "2", "title": "Ready"},
            {"id": "3", "title": "Work in progress"},
            {"id": "4", "title": "Done"},
        ]
        calls = []

        def fake_urlopen(req, timeout=None):
            body = json.loads(req.data)
            calls.append((body["method"], body["params"]))
            if body["method"] == "getColumns":
                return _rpc_response(columns)
            return _rpc_response(True)

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=fake_urlopen):
            added = pk.reconcile_columns("http://x/jsonrpc.php", "tok", 1)

        assert ("updateColumn", ["1", "Todo"]) in calls
        assert ("updateColumn", ["3", "In Progress"]) in calls
        assert ("addColumn", [1, "Waiting for Human"]) in calls
        assert ("addColumn", [1, "Blocked"]) in calls
        assert set(added) == {"Waiting for Human", "Blocked"}
        # "Ready" and "Done" already matched — never touched
        assert not any(c[1] and c[1][0] == "2" for c in calls if c[0] == "updateColumn")

    def test_idempotent_noop_when_all_required_columns_present(self):
        columns = [
            {"id": "1", "title": "Todo"},
            {"id": "2", "title": "Ready"},
            {"id": "3", "title": "In Progress"},
            {"id": "4", "title": "Waiting for Human"},
            {"id": "5", "title": "Blocked"},
            {"id": "6", "title": "Done"},
        ]
        calls = []

        def fake_urlopen(req, timeout=None):
            body = json.loads(req.data)
            calls.append(body["method"])
            if body["method"] == "getColumns":
                return _rpc_response(columns)
            return _rpc_response(True)

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=fake_urlopen):
            added = pk.reconcile_columns("http://x/jsonrpc.php", "tok", 1)

        assert added == []
        assert "updateColumn" not in calls
        assert "addColumn" not in calls

    def test_skips_rename_when_target_already_exists(self):
        """Partial re-run: 'Todo' already added manually, 'Backlog' still there too."""
        columns = [
            {"id": "1", "title": "Backlog"},
            {"id": "2", "title": "Todo"},
            {"id": "3", "title": "Ready"},
            {"id": "4", "title": "Done"},
        ]
        calls = []

        def fake_urlopen(req, timeout=None):
            body = json.loads(req.data)
            calls.append((body["method"], body["params"]))
            if body["method"] == "getColumns":
                return _rpc_response(columns)
            return _rpc_response(True)

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=fake_urlopen):
            pk.reconcile_columns("http://x/jsonrpc.php", "tok", 1)

        # Must not try to rename Backlog->Todo again since Todo exists
        assert not any(m == "updateColumn" and params[1] == "Todo" for m, params in calls)

    def test_only_renames_present_defaults(self):
        """Only 'Backlog' present (not 'Work in progress') — one rename only."""
        columns = [
            {"id": "1", "title": "Backlog"},
            {"id": "2", "title": "Ready"},
            {"id": "3", "title": "Done"},
        ]
        calls = []

        def fake_urlopen(req, timeout=None):
            body = json.loads(req.data)
            calls.append((body["method"], body["params"]))
            if body["method"] == "getColumns":
                return _rpc_response(columns)
            return _rpc_response(True)

        with patch("provision_kanboard.urllib.request.urlopen", side_effect=fake_urlopen):
            pk.reconcile_columns("http://x/jsonrpc.php", "tok", 1)

        update_calls = [c for c in calls if c[0] == "updateColumn"]
        assert len(update_calls) == 1
        assert update_calls[0][1] == ["1", "Todo"]


class TestMain:
    def test_prints_project_id_and_returns_zero_on_success(self, capsys):
        with patch("provision_kanboard.find_or_create_project", return_value=5), patch(
            "provision_kanboard.reconcile_columns", return_value=[]
        ):
            rc = pk.main(["--url", "http://x/jsonrpc.php", "--token", "tok", "--project-name", "P"])

        assert rc == 0
        assert capsys.readouterr().out.strip() == "5"

    def test_returns_one_on_auth_error(self, capsys):
        with patch(
            "provision_kanboard.find_or_create_project",
            side_effect=pk.KanboardAuthError("bad token"),
        ):
            rc = pk.main(["--url", "http://x/jsonrpc.php", "--token", "bad", "--project-name", "P"])

        assert rc == 1
        assert "bad token" in capsys.readouterr().err

    def test_returns_one_on_rpc_error(self, capsys):
        with patch(
            "provision_kanboard.find_or_create_project",
            side_effect=pk.KanboardRPCError("connection failed"),
        ):
            rc = pk.main(["--url", "http://x/jsonrpc.php", "--token", "tok", "--project-name", "P"])

        assert rc == 1
