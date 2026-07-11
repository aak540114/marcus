"""
Unit tests for src/workflows/project_sync_workflow.py

ProjectSyncWorkflow reacts to ``project.created`` events by creating a Gitea
repo and persisting the Kanboard-project → Gitea-repo mapping.  All external
collaborators (GiteaManager, Events, disk I/O) are mocked or redirected to
tmp_path.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.workflows.project_sync_workflow import ProjectSyncWorkflow


def _make_event(pid: int, name: str, description: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        data={
            "kanboard_project_id": pid,
            "project_name": name,
            "project_description": description,
        }
    )


@pytest.fixture()
def gitea_mgr():
    mgr = MagicMock()
    mgr.create_repo = AsyncMock(return_value="http://localhost:3000/root/shopping-cart.git")
    mgr.init_with_readme = AsyncMock(return_value=None)
    return mgr


@pytest.fixture()
def workflow(tmp_path, gitea_mgr):
    events = MagicMock()
    events.subscribe = MagicMock()
    return ProjectSyncWorkflow(
        gitea_manager=gitea_mgr,
        events=events,
        repos_path=str(tmp_path / "project_repos.json"),
        local_repos_base=str(tmp_path / "repos"),
    )


class TestSubscribe:
    def test_subscribes_to_project_created(self, workflow):
        workflow.subscribe()
        workflow._events.subscribe.assert_called_once_with(
            "project.created", workflow._on_project_created
        )


class TestOnProjectCreated:
    @pytest.mark.asyncio
    async def test_creates_repo_and_persists_mapping(self, workflow, gitea_mgr):
        await workflow._on_project_created(_make_event(1, "Shopping Cart", "desc"))

        gitea_mgr.create_repo.assert_called_once_with("Shopping Cart", "desc")
        gitea_mgr.init_with_readme.assert_called_once()

        mapping = workflow.get_repo_for_project(1)
        assert mapping is not None
        assert mapping["gitea_repo_url"] == "http://localhost:3000/root/shopping-cart.git"
        assert mapping["kanboard_project_name"] == "Shopping Cart"
        assert mapping["local_repo_path"].endswith("shopping-cart")

    @pytest.mark.asyncio
    async def test_persists_mapping_to_disk(self, workflow, tmp_path):
        await workflow._on_project_created(_make_event(1, "Shopping Cart"))

        raw = json.loads((tmp_path / "project_repos.json").read_text())
        assert raw["kanboard:1"]["gitea_repo_url"] == (
            "http://localhost:3000/root/shopping-cart.git"
        )

    @pytest.mark.asyncio
    async def test_duplicate_project_created_is_skipped(self, workflow, gitea_mgr):
        await workflow._on_project_created(_make_event(1, "Shopping Cart"))
        gitea_mgr.create_repo.reset_mock()

        await workflow._on_project_created(_make_event(1, "Shopping Cart"))

        gitea_mgr.create_repo.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_repo_failure_does_not_persist_mapping(self, workflow, gitea_mgr):
        gitea_mgr.create_repo = AsyncMock(side_effect=RuntimeError("Gitea unreachable"))

        await workflow._on_project_created(_make_event(2, "Other Project"))

        assert workflow.get_repo_for_project(2) is None

    @pytest.mark.asyncio
    async def test_init_with_readme_failure_does_not_persist_mapping(
        self, workflow, gitea_mgr
    ):
        gitea_mgr.init_with_readme = AsyncMock(side_effect=RuntimeError("push failed"))

        await workflow._on_project_created(_make_event(3, "Third Project"))

        assert workflow.get_repo_for_project(3) is None


class TestGetRepoForProject:
    def test_returns_none_when_unmapped(self, workflow):
        assert workflow.get_repo_for_project(999) is None

    @pytest.mark.asyncio
    async def test_all_mappings_returns_copy(self, workflow):
        await workflow._on_project_created(_make_event(1, "Shopping Cart"))
        mappings = workflow.all_mappings()
        mappings["kanboard:1"]["gitea_repo_url"] = "mutated"
        assert workflow.get_repo_for_project(1)["gitea_repo_url"] != "mutated"


class TestLoadMapping:
    def test_loads_existing_mapping_from_disk(self, tmp_path, gitea_mgr):
        repos_path = tmp_path / "project_repos.json"
        repos_path.write_text(
            json.dumps(
                {
                    "kanboard:5": {
                        "kanboard_project_id": 5,
                        "kanboard_project_name": "Existing",
                        "gitea_repo_url": "http://localhost:3000/root/existing.git",
                        "local_repo_path": "./repos/existing",
                    }
                }
            )
        )
        events = MagicMock()
        wf = ProjectSyncWorkflow(
            gitea_manager=gitea_mgr,
            events=events,
            repos_path=str(repos_path),
            local_repos_base=str(tmp_path / "repos"),
        )
        assert wf.get_repo_for_project(5)["gitea_repo_url"] == (
            "http://localhost:3000/root/existing.git"
        )

    def test_missing_file_starts_empty(self, tmp_path, gitea_mgr):
        events = MagicMock()
        wf = ProjectSyncWorkflow(
            gitea_manager=gitea_mgr,
            events=events,
            repos_path=str(tmp_path / "does_not_exist.json"),
            local_repos_base=str(tmp_path / "repos"),
        )
        assert wf.all_mappings() == {}

    def test_corrupt_file_starts_empty(self, tmp_path, gitea_mgr):
        repos_path = tmp_path / "project_repos.json"
        repos_path.write_text("NOT JSON {{{")
        events = MagicMock()
        wf = ProjectSyncWorkflow(
            gitea_manager=gitea_mgr,
            events=events,
            repos_path=str(repos_path),
            local_repos_base=str(tmp_path / "repos"),
        )
        assert wf.all_mappings() == {}
