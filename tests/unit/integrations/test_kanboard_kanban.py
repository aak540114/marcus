"""Unit tests for src/integrations/providers/kanboard_kanban.py"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.models import TaskStatus, Priority
from src.integrations.providers.kanboard_kanban import KanboardKanban


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config():
    """Minimal valid KanboardKanban configuration."""
    return {
        "kanboard_url": "http://localhost:8080/jsonrpc.php",
        "kanboard_api_token": "test-token",
        "kanboard_project_id": 1,
    }


@pytest.fixture
def provider(config):
    """KanboardKanban instance (not connected)."""
    return KanboardKanban(config)


@pytest.fixture
def connected_provider(config):
    """KanboardKanban with a mocked HTTP client and pre-built column maps."""
    p = KanboardKanban(config)
    # Simulate what connect() would build from getColumns
    p._column_map = {
        "todo": 1,
        "ready": 2,
        "in progress": 3,
        "waiting for human": 4,
        "blocked": 5,
        "done": 6,
    }
    p._column_status = {
        1: TaskStatus.TODO,
        2: TaskStatus.READY,
        3: TaskStatus.IN_PROGRESS,
        4: TaskStatus.WAITING_FOR_HUMAN,
        5: TaskStatus.BLOCKED,
        6: TaskStatus.DONE,
    }
    p._api_user_id = 42
    # Give it a mock client so _rpc() doesn't raise "not connected"
    p._client = AsyncMock()
    return p


# ---------------------------------------------------------------------------
# _rpc helper
# ---------------------------------------------------------------------------


class TestRpcHelper:
    """Tests for the private _rpc JSON-RPC 2.0 dispatcher."""

    @pytest.mark.asyncio
    async def test_rpc_raises_when_not_connected(self, provider):
        """_rpc raises RuntimeError if connect() was never called."""
        with pytest.raises(RuntimeError, match="not connected"):
            await provider._rpc("getMe")

    @pytest.mark.asyncio
    async def test_rpc_returns_result_field(self, connected_provider):
        """_rpc returns the 'result' value from the JSON body."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": {"id": 7}}
        connected_provider._client.post = AsyncMock(return_value=mock_resp)

        result = await connected_provider._rpc("getMe")
        assert result == {"id": 7}

    @pytest.mark.asyncio
    async def test_rpc_raises_on_error_field(self, connected_provider):
        """_rpc raises RuntimeError when the response contains an 'error' key."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32600, "message": "Invalid request"},
        }
        connected_provider._client.post = AsyncMock(return_value=mock_resp)

        with pytest.raises(RuntimeError, match="Kanboard RPC error"):
            await connected_provider._rpc("badMethod")


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------


class TestConnect:
    """Tests for connect()."""

    @pytest.mark.asyncio
    async def test_connect_success_builds_column_map(self, config):
        """connect() calls getProjectById + getColumns and builds _column_map."""
        p = KanboardKanban(config)

        project_result = {"id": 1, "name": "Test Project"}
        columns_result = [
            {"id": "2", "title": "Ready"},
            {"id": "3", "title": "In Progress"},
            {"id": "6", "title": "Done"},
        ]
        me_result = {"id": 42, "username": "jsonrpc"}

        async def fake_rpc(method, **params):
            if method == "getProjectById":
                return project_result
            if method == "getColumns":
                return columns_result
            if method == "getMe":
                return me_result
            return None

        with patch.object(p, "_rpc", side_effect=fake_rpc):
            with patch("httpx.AsyncClient"):
                p._client = AsyncMock()
                ok = await p.connect()

        assert ok is True
        assert p._column_map["ready"] == 2
        assert p._column_map["in progress"] == 3
        assert p._column_status[3] == TaskStatus.IN_PROGRESS
        assert p._api_user_id == 42

    @pytest.mark.asyncio
    async def test_connect_failure_when_project_not_found(self, config):
        """connect() returns False when getProjectById returns False/None."""
        p = KanboardKanban(config)

        async def fake_rpc(method, **params):
            if method == "getProjectById":
                return False
            return None

        with patch.object(p, "_rpc", side_effect=fake_rpc):
            with patch("httpx.AsyncClient"):
                p._client = AsyncMock()
                ok = await p.connect()

        assert ok is False


# ---------------------------------------------------------------------------
# get_all_tasks()
# ---------------------------------------------------------------------------


class TestGetAllTasks:
    """Tests for get_all_tasks()."""

    @pytest.mark.asyncio
    async def test_returns_active_and_closed_tasks(self, connected_provider):
        """get_all_tasks() fetches both status_id=1 and status_id=0."""
        active_kb = [
            {
                "id": "10",
                "title": "Open task",
                "description": "",
                "column_id": "3",
                "owner_id": "5",
                "color_id": "blue",
                "tags": [],
            }
        ]
        closed_kb = [
            {
                "id": "11",
                "title": "Done task",
                "description": "",
                "column_id": "6",
                "owner_id": "0",
                "color_id": "green",
                "tags": [],
            }
        ]

        async def fake_rpc(method, **params):
            if method == "getAllTasks":
                return active_kb if params.get("status_id") == 1 else closed_kb
            if method == "getAllComments":
                return []
            return None

        with patch.object(connected_provider, "_rpc", side_effect=fake_rpc):
            tasks = await connected_provider.get_all_tasks()

        assert len(tasks) == 2
        ids = {t.id for t in tasks}
        assert "10" in ids
        assert "11" in ids

    @pytest.mark.asyncio
    async def test_task_status_mapped_from_column(self, connected_provider):
        """Column ID is translated to the correct TaskStatus."""
        kb_task = {
            "id": "20",
            "title": "In progress task",
            "description": "",
            "column_id": "3",
            "owner_id": "7",
            "color_id": "blue",
            "tags": [],
        }

        async def fake_rpc(method, **params):
            if method == "getAllTasks":
                return [kb_task] if params.get("status_id") == 1 else []
            if method == "getAllComments":
                return []
            return None

        with patch.object(connected_provider, "_rpc", side_effect=fake_rpc):
            tasks = await connected_provider.get_all_tasks()

        assert len(tasks) == 1
        assert tasks[0].status == TaskStatus.IN_PROGRESS
        assert tasks[0].assigned_to == "7"


# ---------------------------------------------------------------------------
# get_available_tasks()
# ---------------------------------------------------------------------------


class TestGetAvailableTasks:
    """Tests for get_available_tasks()."""

    @pytest.mark.asyncio
    async def test_returns_only_unassigned_todo_or_ready(self, connected_provider):
        """get_available_tasks excludes assigned tasks and non-TODO/READY tasks."""
        all_tasks_mock = [
            MagicMock(status=TaskStatus.TODO, assigned_to=None),
            MagicMock(status=TaskStatus.READY, assigned_to=None),
            MagicMock(status=TaskStatus.READY, assigned_to="alice"),  # assigned
            MagicMock(status=TaskStatus.IN_PROGRESS, assigned_to=None),  # wrong status
        ]

        with patch.object(
            connected_provider, "get_all_tasks", AsyncMock(return_value=all_tasks_mock)
        ):
            available = await connected_provider.get_available_tasks()

        assert len(available) == 2
        for t in available:
            assert t.assigned_to is None
            assert t.status in (TaskStatus.TODO, TaskStatus.READY)


# ---------------------------------------------------------------------------
# move_task_to_column()
# ---------------------------------------------------------------------------


class TestMoveTaskToColumn:
    """Tests for move_task_to_column()."""

    @pytest.mark.asyncio
    async def test_success(self, connected_provider):
        """move_task_to_column calls moveTaskPosition with correct column_id."""
        calls = []

        async def fake_rpc(method, **params):
            calls.append((method, params))
            return True

        with patch.object(connected_provider, "_rpc", side_effect=fake_rpc):
            ok = await connected_provider.move_task_to_column("99", "in progress")

        assert ok is True
        assert calls[0][0] == "moveTaskPosition"
        assert calls[0][1]["column_id"] == 3

    @pytest.mark.asyncio
    async def test_unknown_column_raises_value_error(self, connected_provider):
        """move_task_to_column raises ValueError for unknown column names."""
        with pytest.raises(ValueError, match="not found"):
            await connected_provider.move_task_to_column("99", "nonexistent column")


# ---------------------------------------------------------------------------
# add_comment()
# ---------------------------------------------------------------------------


class TestAddComment:
    """Tests for add_comment()."""

    @pytest.mark.asyncio
    async def test_calls_create_comment(self, connected_provider):
        """add_comment calls createComment with task_id and user_id."""
        calls = []

        async def fake_rpc(method, **params):
            calls.append((method, params))
            return 1  # comment ID

        with patch.object(connected_provider, "_rpc", side_effect=fake_rpc):
            ok = await connected_provider.add_comment("55", "Hello Kanboard")

        assert ok is True
        assert calls[0][0] == "createComment"
        assert calls[0][1]["task_id"] == 55
        assert calls[0][1]["user_id"] == 42
        assert calls[0][1]["content"] == "Hello Kanboard"


# ---------------------------------------------------------------------------
# assign_task()
# ---------------------------------------------------------------------------


class TestAssignTask:
    """Tests for assign_task()."""

    @pytest.mark.asyncio
    async def test_calls_update_task_with_owner_id(self, connected_provider):
        """assign_task sends updateTask with owner_id."""
        calls = []

        async def fake_rpc(method, **params):
            calls.append((method, params))
            return True

        with patch.object(connected_provider, "_rpc", side_effect=fake_rpc):
            ok = await connected_provider.assign_task("77", "3")

        assert ok is True
        assert calls[0][0] == "updateTask"
        assert calls[0][1]["owner_id"] == 3
        assert calls[0][1]["id"] == 77


# ---------------------------------------------------------------------------
# report_blocker()
# ---------------------------------------------------------------------------


class TestReportBlocker:
    """Tests for report_blocker()."""

    @pytest.mark.asyncio
    async def test_moves_to_blocked_and_posts_comment(self, connected_provider):
        """report_blocker moves to 'blocked' column and posts a BLOCKED comment."""
        move_calls = []
        comment_calls = []

        async def fake_move(task_id, column_name):
            move_calls.append((task_id, column_name))
            return True

        async def fake_comment(task_id, comment):
            comment_calls.append((task_id, comment))
            return True

        with (
            patch.object(
                connected_provider, "move_task_to_column", side_effect=fake_move
            ),
            patch.object(
                connected_provider, "add_comment", side_effect=fake_comment
            ),
        ):
            ok = await connected_provider.report_blocker("33", "waiting on design doc")

        assert ok is True
        assert move_calls[0] == ("33", "blocked")
        assert "BLOCKED" in comment_calls[0][1]
        assert "waiting on design doc" in comment_calls[0][1]


# ---------------------------------------------------------------------------
# disconnect()
# ---------------------------------------------------------------------------


class TestDisconnect:
    """Tests for disconnect()."""

    @pytest.mark.asyncio
    async def test_closes_client(self, connected_provider):
        """disconnect() calls aclose() on the underlying client."""
        aclose_mock = AsyncMock()
        connected_provider._client.aclose = aclose_mock

        await connected_provider.disconnect()

        aclose_mock.assert_awaited_once()
        assert connected_provider._client is None
