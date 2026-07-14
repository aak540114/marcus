"""
Unit tests for ``_wire_human_gated_workflow`` in src/marcus_mcp/server.py

This is the function that constructs GiteaManager -> ProjectSyncWorkflow ->
HumanGatedWorkflow at HTTP-server startup and registers the workflow so the
human-gated MCP tools (get_work_context, signal_ready_for_review, etc.)
stop returning "HumanGatedWorkflow not initialised". All collaborators
(GiteaManager, HumanGatedWorkflow, ProjectSyncWorkflow, register_workflow)
are patched — no real network or subprocess calls.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.marcus_mcp.server import (
    _get_dev_env_settings_mgr,
    _resolve_ticket_branch,
    _resolve_ticket_repo_path,
    _wire_human_gated_workflow,
)


def _make_server(events=MagicMock(), kanban_client=MagicMock(), provider="kanboard"):
    return SimpleNamespace(events=events, kanban_client=kanban_client, provider=provider)


class TestSkipsWhenPrerequisitesMissing:
    @pytest.mark.asyncio
    async def test_skips_when_events_is_none(self):
        server = _make_server(events=None)
        with patch("src.workflows.human_gated_workflow.HumanGatedWorkflow") as wf_cls:
            await _wire_human_gated_workflow(server)
        wf_cls.assert_not_called()
        assert not hasattr(server, "_human_gated_workflow")

    @pytest.mark.asyncio
    async def test_skips_when_kanban_client_is_none(self):
        server = _make_server(kanban_client=None)
        with patch("src.workflows.human_gated_workflow.HumanGatedWorkflow") as wf_cls:
            await _wire_human_gated_workflow(server)
        wf_cls.assert_not_called()
        assert not hasattr(server, "_human_gated_workflow")


class TestConstructsWorkflowWithoutGitea:
    @pytest.mark.asyncio
    async def test_starts_workflow_when_gitea_env_vars_absent(self, monkeypatch):
        monkeypatch.delenv("GITEA_URL", raising=False)
        monkeypatch.delenv("GITEA_TOKEN", raising=False)
        server = _make_server()

        mock_workflow = AsyncMock()
        with (
            patch(
                "src.workflows.human_gated_workflow.HumanGatedWorkflow",
                return_value=mock_workflow,
            ) as wf_cls,
            patch("src.marcus_mcp.tools.human_gated.register_workflow") as register,
            patch("src.integrations.gitea_manager.GiteaManager") as gitea_cls,
        ):
            await _wire_human_gated_workflow(server)

        gitea_cls.assert_not_called()
        wf_cls.assert_called_once()
        kwargs = wf_cls.call_args.kwargs
        assert kwargs["kanban"] is server.kanban_client
        assert kwargs["events"] is server.events
        assert kwargs["provider_name"] == "kanboard"
        assert kwargs["project_sync"] is None
        register.assert_called_once_with(mock_workflow)
        mock_workflow.start.assert_awaited_once()
        assert server._human_gated_workflow is mock_workflow
        assert server._project_sync is None


class TestConstructsWorkflowWithGitea:
    @pytest.mark.asyncio
    async def test_wires_project_sync_and_webhook_when_gitea_configured(self, monkeypatch):
        monkeypatch.setenv("GITEA_URL", "http://gitea:3000")
        monkeypatch.setenv("GITEA_TOKEN", "tok123")
        monkeypatch.setenv("GITEA_WEBHOOK_TOKEN", "s3cret")
        server = _make_server()

        mock_gitea = AsyncMock()
        mock_gitea.connect = AsyncMock(return_value=True)
        mock_workflow = AsyncMock()

        with (
            patch(
                "src.integrations.gitea_manager.GiteaManager", return_value=mock_gitea
            ) as gitea_cls,
            patch(
                "src.workflows.project_sync_workflow.ProjectSyncWorkflow"
            ) as sync_cls,
            patch(
                "src.workflows.human_gated_workflow.HumanGatedWorkflow",
                return_value=mock_workflow,
            ) as wf_cls,
            patch("src.marcus_mcp.tools.human_gated.register_workflow"),
        ):
            sync_instance = sync_cls.return_value
            await _wire_human_gated_workflow(server)

        gitea_cls.assert_called_once_with(
            gitea_url="http://gitea:3000", token="tok123", namespace=None
        )
        mock_gitea.connect.assert_awaited_once()
        sync_cls.assert_called_once()
        sync_kwargs = sync_cls.call_args.kwargs
        assert sync_kwargs["webhook_secret"] == "s3cret"
        assert sync_kwargs["webhook_target_url"] == "http://marcus:4298/webhooks/gitea"
        sync_instance.subscribe.assert_called_once()
        wf_kwargs = wf_cls.call_args.kwargs
        assert wf_kwargs["project_sync"] is sync_instance
        assert server._gitea_manager is mock_gitea

    @pytest.mark.asyncio
    async def test_no_webhook_target_when_secret_not_configured(self, monkeypatch):
        monkeypatch.setenv("GITEA_URL", "http://gitea:3000")
        monkeypatch.setenv("GITEA_TOKEN", "tok123")
        monkeypatch.delenv("GITEA_WEBHOOK_TOKEN", raising=False)
        server = _make_server()

        mock_gitea = AsyncMock()
        mock_gitea.connect = AsyncMock(return_value=True)

        with (
            patch("src.integrations.gitea_manager.GiteaManager", return_value=mock_gitea),
            patch("src.workflows.project_sync_workflow.ProjectSyncWorkflow") as sync_cls,
            patch("src.workflows.human_gated_workflow.HumanGatedWorkflow", return_value=AsyncMock()),
            patch("src.marcus_mcp.tools.human_gated.register_workflow"),
        ):
            await _wire_human_gated_workflow(server)

        sync_kwargs = sync_cls.call_args.kwargs
        assert sync_kwargs["webhook_secret"] is None
        assert sync_kwargs["webhook_target_url"] is None

    @pytest.mark.asyncio
    async def test_falls_back_to_no_project_sync_when_gitea_connect_fails(self, monkeypatch):
        monkeypatch.setenv("GITEA_URL", "http://gitea:3000")
        monkeypatch.setenv("GITEA_TOKEN", "tok123")
        server = _make_server()

        mock_gitea = AsyncMock()
        mock_gitea.connect = AsyncMock(return_value=False)
        mock_workflow = AsyncMock()

        with (
            patch("src.integrations.gitea_manager.GiteaManager", return_value=mock_gitea),
            patch("src.workflows.project_sync_workflow.ProjectSyncWorkflow") as sync_cls,
            patch(
                "src.workflows.human_gated_workflow.HumanGatedWorkflow",
                return_value=mock_workflow,
            ) as wf_cls,
            patch("src.marcus_mcp.tools.human_gated.register_workflow"),
        ):
            await _wire_human_gated_workflow(server)

        sync_cls.assert_not_called()
        wf_kwargs = wf_cls.call_args.kwargs
        assert wf_kwargs["project_sync"] is None
        assert not hasattr(server, "_gitea_manager")


class TestSharesDevEnvManagerSingleton:
    @pytest.mark.asyncio
    async def test_reuses_existing_dev_env_manager(self, monkeypatch):
        monkeypatch.delenv("GITEA_URL", raising=False)
        server = _make_server()
        existing_dev_mgr = MagicMock()
        server._dev_env_manager = existing_dev_mgr

        with (
            patch(
                "src.workflows.human_gated_workflow.HumanGatedWorkflow",
                return_value=AsyncMock(),
            ) as wf_cls,
            patch("src.marcus_mcp.tools.human_gated.register_workflow"),
            patch("src.core.dev_environment.DevEnvironmentManager") as dev_env_cls,
        ):
            await _wire_human_gated_workflow(server)

        dev_env_cls.assert_not_called()
        assert wf_cls.call_args.kwargs["dev_env_manager"] is existing_dev_mgr


class TestGetDevEnvSettingsMgr:
    """_get_dev_env_settings_mgr must return one shared instance per server.

    DevEnvSettingsManager caches its value in memory after the first disk
    read — a second, independently-constructed instance would silently
    diverge from a human's live change via /api/dev-env-setting, so every
    caller (both DevEnvironmentManager construction sites and the API
    route) must resolve to the exact same object.
    """

    def test_constructs_once_and_caches_on_server(self):
        server = SimpleNamespace()
        with patch("src.core.dev_env_settings.DevEnvSettingsManager") as mgr_cls:
            mgr_cls.return_value = MagicMock()
            first = _get_dev_env_settings_mgr(server)
            second = _get_dev_env_settings_mgr(server)

        mgr_cls.assert_called_once()
        assert first is second
        assert server._dev_env_settings_mgr is first

    def test_reuses_a_preexisting_instance(self):
        server = SimpleNamespace()
        existing = MagicMock()
        server._dev_env_settings_mgr = existing

        with patch("src.core.dev_env_settings.DevEnvSettingsManager") as mgr_cls:
            result = _get_dev_env_settings_mgr(server)

        mgr_cls.assert_not_called()
        assert result is existing


class TestResolveTicketBranch:
    """_resolve_ticket_branch prefers the lifecycle record over convention."""

    def test_no_human_gated_workflow_uses_convention(self):
        server = SimpleNamespace()
        assert _resolve_ticket_branch(server, "42", "kanboard") == "ticket/kanboard/42"

    def test_no_lifecycle_record_uses_convention(self):
        workflow = MagicMock()
        workflow._lifecycle.get = MagicMock(return_value=None)
        server = SimpleNamespace(_human_gated_workflow=workflow)
        assert _resolve_ticket_branch(server, "42", "kanboard") == "ticket/kanboard/42"

    def test_uses_the_records_real_branch_name(self):
        record = SimpleNamespace(branch_name="feature/custom-branch")
        workflow = MagicMock()
        workflow._lifecycle.get = MagicMock(return_value=record)
        server = SimpleNamespace(_human_gated_workflow=workflow)
        assert _resolve_ticket_branch(server, "42", "kanboard") == "feature/custom-branch"

    def test_empty_branch_name_on_record_falls_back_to_convention(self):
        record = SimpleNamespace(branch_name="")
        workflow = MagicMock()
        workflow._lifecycle.get = MagicMock(return_value=record)
        server = SimpleNamespace(_human_gated_workflow=workflow)
        assert _resolve_ticket_branch(server, "42", "kanboard") == "ticket/kanboard/42"


class TestResolveTicketRepoPath:
    """_resolve_ticket_repo_path resolves (or on-demand provisions) a repo path."""

    @pytest.mark.asyncio
    async def test_no_project_sync_returns_none(self):
        server = SimpleNamespace(_project_sync=None, kanban_client=MagicMock())
        assert await _resolve_ticket_repo_path(server, "1") is None

    @pytest.mark.asyncio
    async def test_empty_project_id_returns_none(self):
        server = SimpleNamespace(_project_sync=MagicMock(), kanban_client=MagicMock())
        assert await _resolve_ticket_repo_path(server, "") is None

    @pytest.mark.asyncio
    async def test_non_numeric_project_id_returns_none(self):
        server = SimpleNamespace(_project_sync=MagicMock(), kanban_client=MagicMock())
        assert await _resolve_ticket_repo_path(server, "not-a-number") is None

    @pytest.mark.asyncio
    async def test_returns_cached_mapping_without_provisioning(self):
        project_sync = MagicMock()
        project_sync.get_repo_for_project = MagicMock(
            return_value={"local_repo_path": "./data/repos/shopping-cart"}
        )
        project_sync.ensure_repo = AsyncMock()
        server = SimpleNamespace(_project_sync=project_sync, kanban_client=MagicMock())

        result = await _resolve_ticket_repo_path(server, "1")

        assert result == "./data/repos/shopping-cart"
        project_sync.ensure_repo.assert_not_called()

    @pytest.mark.asyncio
    async def test_provisions_on_demand_when_no_mapping_and_name_resolvable(self):
        project_sync = MagicMock()
        project_sync.get_repo_for_project = MagicMock(return_value=None)
        project_sync.ensure_repo = AsyncMock(
            return_value={"local_repo_path": "./data/repos/new-project"}
        )
        kanban = MagicMock()
        kanban.get_project_name = AsyncMock(return_value="New Project")
        server = SimpleNamespace(_project_sync=project_sync, kanban_client=kanban)

        result = await _resolve_ticket_repo_path(server, "5")

        project_sync.ensure_repo.assert_awaited_once_with(5, "New Project")
        assert result == "./data/repos/new-project"

    @pytest.mark.asyncio
    async def test_kanban_without_get_project_name_returns_none(self):
        project_sync = MagicMock()
        project_sync.get_repo_for_project = MagicMock(return_value=None)
        project_sync.ensure_repo = AsyncMock()
        kanban = MagicMock(spec=[])  # no get_project_name attribute
        server = SimpleNamespace(_project_sync=project_sync, kanban_client=kanban)

        result = await _resolve_ticket_repo_path(server, "5")

        assert result is None
        project_sync.ensure_repo.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_project_name_failure_returns_none(self):
        project_sync = MagicMock()
        project_sync.get_repo_for_project = MagicMock(return_value=None)
        project_sync.ensure_repo = AsyncMock()
        kanban = MagicMock()
        kanban.get_project_name = AsyncMock(side_effect=RuntimeError("kanban down"))
        server = SimpleNamespace(_project_sync=project_sync, kanban_client=kanban)

        result = await _resolve_ticket_repo_path(server, "5")

        assert result is None
        project_sync.ensure_repo.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_repo_failure_returns_none(self):
        project_sync = MagicMock()
        project_sync.get_repo_for_project = MagicMock(return_value=None)
        project_sync.ensure_repo = AsyncMock(return_value=None)
        kanban = MagicMock()
        kanban.get_project_name = AsyncMock(return_value="New Project")
        server = SimpleNamespace(_project_sync=project_sync, kanban_client=kanban)

        result = await _resolve_ticket_repo_path(server, "5")

        assert result is None
