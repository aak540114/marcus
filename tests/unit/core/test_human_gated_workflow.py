"""
Unit tests for HumanGatedWorkflow.

Covers the human-gated AI workflow rules:
  - AI starts when a ticket IS assigned to a human AND status ≠ todo.
  - When a human assigns themselves, AI starts work if the column is already
    past todo; if the column is still todo, AI waits for the next status change.
  - When status changes to ready/in_progress AND a human is assigned, AI starts.
  - When a ticket is unassigned, the AI claim is released and AI stops.
  - Humans cannot push a card to waiting_for_human (AI-only state).
  - The claim gate prevents two Marcus instances from double-starting.
  - get_work_context includes the already_claimed_by field.

All external dependencies (kanban, branch manager, dev env, AC generator)
are mocked; no file I/O or network calls occur.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.events import Events
from src.core.ticket_lifecycle import (
    InvalidTransitionError,
    TicketLifecycleManager,
    TicketState,
)
from src.workflows.human_gated_workflow import HumanGatedWorkflow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(data: dict) -> Any:
    """Build a minimal event object with a .data attribute."""
    ev = MagicMock()
    ev.data = data
    return ev


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_file(tmp_path):
    """Temporary lifecycle state file."""
    return str(tmp_path / "lifecycle.json")


@pytest.fixture
def lifecycle(state_file):
    """Fresh lifecycle manager backed by a temp file."""
    return TicketLifecycleManager(state_file=state_file)


@pytest.fixture
def mock_kanban():
    """Mock KanbanInterface."""
    kb = MagicMock()
    kb.move_task_to_column = AsyncMock(return_value=True)
    kb.add_comment = AsyncMock(return_value=1)
    kb.get_task_by_id = AsyncMock(return_value=None)
    return kb


@pytest.fixture
def mock_branch():
    """Mock BranchManager."""
    bm = MagicMock()
    bm.create_branch = AsyncMock(return_value=True)
    bm.merge_to_main = AsyncMock(return_value=True)
    bm.rebase_on_main = AsyncMock(return_value=True)
    bm.get_branch_commits = AsyncMock(return_value=[])
    bm.config = MagicMock()
    bm.config.main_branch = "main"
    bm.make_branch_name = MagicMock(
        side_effect=lambda provider, tid: f"ticket/{provider}/{tid}"
    )
    return bm


@pytest.fixture
def mock_dev_env():
    """Mock DevEnvironmentManager."""
    de = MagicMock()
    de.stop = AsyncMock()
    de.stop_all = AsyncMock()
    de.start = AsyncMock()
    de.get_info = MagicMock(return_value=None)
    return de


@pytest.fixture
def mock_ac_gen():
    """Mock ACGenerator."""
    gen = MagicMock()
    gen.generate = AsyncMock(return_value="- [ ] Acceptance criterion 1")
    return gen


@pytest.fixture
def workflow(lifecycle, mock_kanban, mock_branch, mock_dev_env, mock_ac_gen):
    """HumanGatedWorkflow wired with mocked dependencies."""
    events = Events()
    wf = HumanGatedWorkflow(
        kanban=mock_kanban,
        events=events,
        provider_name="kanboard",
        lifecycle=lifecycle,
        branch_manager=mock_branch,
        dev_env_manager=mock_dev_env,
        ac_generator=mock_ac_gen,
    )
    with patch(
        "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
        side_effect=lambda provider, tid: f"ticket/{provider}/{tid}",
    ):
        yield wf


# ---------------------------------------------------------------------------
# Trigger: human assigns ticket + status already past todo → AI starts
# ---------------------------------------------------------------------------


class TestAssignedTrigger:
    """Human assigning themselves is the signal for AI to start work."""

    @pytest.mark.asyncio
    async def test_assign_when_ready_starts_ai(
        self, workflow, lifecycle, mock_kanban
    ):
        """Assigning human to a ready ticket causes AI to claim and start."""
        lifecycle.get_or_create("10", "kanboard")
        lifecycle.transition("10", "kanboard", TicketState.READY)

        event = _make_event(
            {"ticket_id": "10", "assignee": "alice", "provider": "kanboard"}
        )
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/10",
        ):
            await workflow._on_ticket_assigned(event)

        rec = lifecycle.get("10", "kanboard")
        assert rec is not None
        assert rec.assignee == "alice"
        assert rec.ai_agent_id is not None
        assert rec.state == TicketState.IN_PROGRESS
        mock_kanban.move_task_to_column.assert_called_with("10", "in progress")

    @pytest.mark.asyncio
    async def test_assign_when_in_progress_starts_ai(
        self, workflow, lifecycle, mock_kanban
    ):
        """Assigning human to an in_progress ticket causes AI to claim it."""
        lifecycle.get_or_create("11", "kanboard")
        lifecycle.transition("11", "kanboard", TicketState.READY)
        lifecycle.transition("11", "kanboard", TicketState.IN_PROGRESS)

        event = _make_event(
            {"ticket_id": "11", "assignee": "bob", "provider": "kanboard"}
        )
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/11",
        ):
            await workflow._on_ticket_assigned(event)

        rec = lifecycle.get("11", "kanboard")
        assert rec is not None
        assert rec.ai_agent_id is not None

    @pytest.mark.asyncio
    async def test_assign_when_todo_does_not_start_ai(
        self, workflow, lifecycle, mock_kanban
    ):
        """Assigning human to a still-todo ticket does NOT start AI."""
        lifecycle.get_or_create("12", "kanboard")

        event = _make_event(
            {"ticket_id": "12", "assignee": "carol", "provider": "kanboard"}
        )
        await workflow._on_ticket_assigned(event)

        rec = lifecycle.get("12", "kanboard")
        assert rec is not None
        assert rec.ai_agent_id is None
        mock_kanban.move_task_to_column.assert_not_called()

    @pytest.mark.asyncio
    async def test_assign_records_human_name(self, workflow, lifecycle):
        """Assignee name is stored on the lifecycle record."""
        lifecycle.get_or_create("13", "kanboard")
        event = _make_event(
            {"ticket_id": "13", "assignee": "dave", "provider": "kanboard"}
        )
        await workflow._on_ticket_assigned(event)

        rec = lifecycle.get("13", "kanboard")
        assert rec is not None
        assert rec.assignee == "dave"


# ---------------------------------------------------------------------------
# Trigger: status changes to ready/in_progress with human owner → AI starts
# ---------------------------------------------------------------------------


class TestStatusChangedTrigger:
    """Status-change event triggers AI only when a human is assigned."""

    @pytest.mark.asyncio
    async def test_ready_with_assignee_starts_ai(
        self, workflow, lifecycle, mock_kanban
    ):
        """Status → ready AND human assigned → AI claims and starts."""
        lifecycle.get_or_create("20", "kanboard")
        lifecycle.set_assignee("20", "kanboard", "alice")

        event = _make_event(
            {"ticket_id": "20", "new_status": "ready", "old_status": "todo",
             "provider": "kanboard"}
        )
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/20",
        ):
            await workflow._on_status_changed(event)

        rec = lifecycle.get("20", "kanboard")
        assert rec is not None
        assert rec.state == TicketState.IN_PROGRESS
        assert rec.ai_agent_id is not None

    @pytest.mark.asyncio
    async def test_ready_without_assignee_does_not_start_ai(
        self, workflow, lifecycle, mock_kanban
    ):
        """Status → ready with NO human assigned → AI does nothing."""
        lifecycle.get_or_create("21", "kanboard")

        event = _make_event(
            {"ticket_id": "21", "new_status": "ready", "old_status": "todo",
             "provider": "kanboard"}
        )
        await workflow._on_status_changed(event)

        rec = lifecycle.get("21", "kanboard")
        assert rec is not None
        assert rec.ai_agent_id is None
        mock_kanban.move_task_to_column.assert_not_called()

    @pytest.mark.asyncio
    async def test_in_progress_with_assignee_starts_ai(
        self, workflow, lifecycle, mock_kanban
    ):
        """Status → in_progress AND human assigned → AI claims."""
        lifecycle.get_or_create("22", "kanboard")
        lifecycle.transition("22", "kanboard", TicketState.READY)
        lifecycle.set_assignee("22", "kanboard", "bob")

        event = _make_event(
            {"ticket_id": "22", "new_status": "in_progress",
             "old_status": "ready", "provider": "kanboard"}
        )
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/22",
        ):
            await workflow._on_status_changed(event)

        rec = lifecycle.get("22", "kanboard")
        assert rec is not None
        assert rec.ai_agent_id is not None

    @pytest.mark.asyncio
    async def test_waiting_for_human_set_by_human_is_rejected(
        self, workflow, lifecycle
    ):
        """Human moving card to waiting_for_human is silently ignored."""
        lifecycle.get_or_create("23", "kanboard")
        lifecycle.transition("23", "kanboard", TicketState.READY)
        lifecycle.transition("23", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.set_assignee("23", "kanboard", "carol")

        event = _make_event(
            {"ticket_id": "23", "new_status": "waiting_for_human",
             "old_status": "in_progress", "provider": "kanboard"}
        )
        await workflow._on_status_changed(event)

        rec = lifecycle.get("23", "kanboard")
        assert rec is not None
        assert rec.state == TicketState.IN_PROGRESS  # unchanged

    @pytest.mark.asyncio
    async def test_todo_status_resets_lifecycle_state(self, workflow, lifecycle):
        """Human moving card to todo updates internal lifecycle state."""
        lifecycle.get_or_create("24", "kanboard")
        lifecycle.transition("24", "kanboard", TicketState.READY)
        lifecycle.set_assignee("24", "kanboard", "dave")

        event = _make_event(
            {"ticket_id": "24", "new_status": "todo",
             "old_status": "ready", "provider": "kanboard"}
        )
        await workflow._on_status_changed(event)

        rec = lifecycle.get("24", "kanboard")
        assert rec is not None
        assert rec.state == TicketState.TODO

    @pytest.mark.asyncio
    async def test_in_progress_from_waiting_for_human_resumes_ai(
        self, workflow, lifecycle, mock_kanban
    ):
        """Human moving card from waiting_for_human to in_progress resumes AI."""
        lifecycle.get_or_create("25", "kanboard")
        lifecycle.transition("25", "kanboard", TicketState.READY)
        lifecycle.transition("25", "kanboard", TicketState.IN_PROGRESS)
        lifecycle.transition("25", "kanboard", TicketState.WAITING_FOR_HUMAN)
        lifecycle.set_assignee("25", "kanboard", "eve")

        event = _make_event(
            {"ticket_id": "25", "new_status": "in_progress",
             "old_status": "waiting_for_human", "provider": "kanboard"}
        )
        await workflow._on_status_changed(event)

        rec = lifecycle.get("25", "kanboard")
        assert rec is not None
        assert rec.state == TicketState.IN_PROGRESS
        # Branch creation not called — AI is resuming, not starting fresh.
        mock_kanban.move_task_to_column.assert_not_called()


# ---------------------------------------------------------------------------
# Trigger: ticket unassigned → AI releases claim and stops
# ---------------------------------------------------------------------------


class TestUnassignedTrigger:
    """When a human unassigns, AI releases its claim and stops."""

    @pytest.mark.asyncio
    async def test_unassign_releases_ai_claim(
        self, workflow, lifecycle, mock_kanban
    ):
        """Unassigning clears the AI claim."""
        lifecycle.get_or_create("30", "kanboard")
        lifecycle.claim_ticket("30", "kanboard", "agent-x")
        lifecycle.set_assignee("30", "kanboard", "alice")

        event = _make_event({"ticket_id": "30", "provider": "kanboard"})
        await workflow._on_ticket_unassigned(event)

        rec = lifecycle.get("30", "kanboard")
        assert rec is not None
        assert rec.ai_agent_id is None
        assert rec.assignee in (None, "", "0")

    @pytest.mark.asyncio
    async def test_unassign_does_not_start_ai(
        self, workflow, lifecycle, mock_kanban
    ):
        """Unassigning never starts AI work."""
        lifecycle.get_or_create("31", "kanboard")
        lifecycle.transition("31", "kanboard", TicketState.READY)
        lifecycle.set_assignee("31", "kanboard", "bob")

        event = _make_event({"ticket_id": "31", "provider": "kanboard"})
        await workflow._on_ticket_unassigned(event)

        mock_kanban.move_task_to_column.assert_not_called()
        rec = lifecycle.get("31", "kanboard")
        assert rec is not None
        assert rec.ai_agent_id is None


# ---------------------------------------------------------------------------
# Anti-duplication: second claim is rejected
# ---------------------------------------------------------------------------


class TestClaimGate:
    """Two concurrent Marcus instances cannot both claim the same ticket."""

    @pytest.mark.asyncio
    async def test_already_claimed_ticket_is_skipped(
        self, workflow, lifecycle, mock_kanban
    ):
        """If a ticket is already claimed, _start_ai_work exits early."""
        lifecycle.get_or_create("40", "kanboard")
        lifecycle.claim_ticket("40", "kanboard", "other-marcus")

        rec = lifecycle.get("40", "kanboard")
        assert rec is not None
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/40",
        ):
            await workflow._start_ai_work("40", rec)

        mock_kanban.move_task_to_column.assert_not_called()
        rec2 = lifecycle.get("40", "kanboard")
        assert rec2 is not None
        assert rec2.ai_agent_id == "other-marcus"  # original holder unchanged

    @pytest.mark.asyncio
    async def test_branch_failure_releases_claim(
        self, workflow, lifecycle, mock_branch
    ):
        """If branch creation fails, the claim is released so retry is possible."""
        mock_branch.create_branch = AsyncMock(return_value=False)
        lifecycle.get_or_create("41", "kanboard")

        rec = lifecycle.get("41", "kanboard")
        assert rec is not None
        with patch(
            "src.workflows.human_gated_workflow.BranchManager.make_branch_name",
            return_value="ticket/kanboard/41",
        ):
            await workflow._start_ai_work("41", rec)

        rec2 = lifecycle.get("41", "kanboard")
        assert rec2 is not None
        assert rec2.ai_agent_id is None  # released after failure


# ---------------------------------------------------------------------------
# get_work_context: includes already_claimed_by
# ---------------------------------------------------------------------------


class TestGetWorkContext:
    """get_work_context exposes the current claimant."""

    @pytest.mark.asyncio
    async def test_unclaimed_ticket_has_none_claimed_by(
        self, workflow, lifecycle
    ):
        """already_claimed_by is None for unclaimed tickets."""
        lifecycle.get_or_create("50", "kanboard")
        ctx = await workflow.get_work_context("50")
        assert ctx is not None
        assert ctx["already_claimed_by"] is None

    @pytest.mark.asyncio
    async def test_claimed_ticket_exposes_agent_id(
        self, workflow, lifecycle
    ):
        """already_claimed_by shows the holding agent's identifier."""
        lifecycle.get_or_create("51", "kanboard")
        lifecycle.claim_ticket("51", "kanboard", "marcus-abc123")
        ctx = await workflow.get_work_context("51")
        assert ctx is not None
        assert ctx["already_claimed_by"] == "marcus-abc123"


# ---------------------------------------------------------------------------
# _is_unassigned helper
# ---------------------------------------------------------------------------


class TestIsUnassigned:
    """_is_unassigned returns True for None, empty string, and '0'."""

    def _make_record(self, assignee):
        """Build a minimal TicketRecord-like mock."""
        rec = MagicMock()
        rec.assignee = assignee
        return rec

    def test_none_assignee_is_unassigned(self, workflow):
        """assignee=None is treated as unassigned."""
        assert workflow._is_unassigned(self._make_record(None)) is True

    def test_empty_string_is_unassigned(self, workflow):
        """assignee='' is treated as unassigned."""
        assert workflow._is_unassigned(self._make_record("")) is True

    def test_kanboard_zero_is_unassigned(self, workflow):
        """Kanboard owner_id '0' sentinel is treated as unassigned."""
        assert workflow._is_unassigned(self._make_record("0")) is True

    def test_named_assignee_is_not_unassigned(self, workflow):
        """A real username is not treated as unassigned."""
        assert workflow._is_unassigned(self._make_record("alice")) is False
