"""Kanboard JSON-RPC 2.0 kanban provider.

Communicates with Kanboard via its JSON-RPC 2.0 API at ``/jsonrpc.php``.
Authentication uses HTTP Basic Auth with the literal username ``jsonrpc``
and the API token obtained from Kanboard → Settings → API.

Configuration keys
------------------
``kanboard_url``
    Full URL to the RPC endpoint, e.g. ``http://localhost:8080/jsonrpc.php``.
``kanboard_api_token``
    API token from Kanboard settings.
``kanboard_project_id``
    Integer project ID to operate on.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

import httpx

from src.core.models import Priority, Task, TaskStatus
from src.integrations.kanban_interface import KanbanInterface, KanbanProvider

logger = logging.getLogger(__name__)

# Maps lower-cased Kanboard column names → TaskStatus
_COLUMN_STATUS_MAP: Dict[str, TaskStatus] = {
    "backlog": TaskStatus.TODO,
    "todo": TaskStatus.TODO,
    "ready": TaskStatus.READY,
    "in progress": TaskStatus.IN_PROGRESS,
    "in_progress": TaskStatus.IN_PROGRESS,
    "waiting for human": TaskStatus.WAITING_FOR_HUMAN,
    "waiting_for_human": TaskStatus.WAITING_FOR_HUMAN,
    "blocked": TaskStatus.BLOCKED,
    "done": TaskStatus.DONE,
    "closed": TaskStatus.DONE,
}

# Kanboard color → Priority (rough mapping)
_COLOR_PRIORITY_MAP: Dict[str, Priority] = {
    "red": Priority.URGENT,
    "orange": Priority.HIGH,
    "yellow": Priority.MEDIUM,
    "blue": Priority.MEDIUM,
    "green": Priority.LOW,
    "purple": Priority.LOW,
    "grey": Priority.LOW,
}


class KanboardKanban(KanbanInterface):
    """Kanboard kanban provider using JSON-RPC 2.0.

    Parameters
    ----------
    config : Dict[str, Any]
        Must contain:
            ``kanboard_url`` — RPC endpoint URL.
            ``kanboard_api_token`` — API token (password for ``jsonrpc`` user).
            ``kanboard_project_id`` — integer project ID.
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        """Initialise the provider (no network calls yet)."""
        super().__init__(config)
        self.provider = KanbanProvider.KANBOARD
        self._rpc_url: str = config["kanboard_url"]
        self._api_token: str = config["kanboard_api_token"]
        self._project_id: int = int(config.get("kanboard_project_id", 1))

        self._client: Optional[httpx.AsyncClient] = None
        self._auth = httpx.BasicAuth("jsonrpc", self._api_token)

        # Built by connect():  {column_title_lower: column_id}
        self._column_map: Dict[str, int] = {}
        # Reverse map: {column_id: TaskStatus}
        self._column_status: Dict[int, TaskStatus] = {}
        self._api_user_id: int = 1

    # ------------------------------------------------------------------
    # KanbanInterface — connection
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Connect to Kanboard and build the column map.

        Returns
        -------
        bool
            True on success, False on failure.
        """
        self._client = httpx.AsyncClient(timeout=30.0)
        try:
            project = await self._rpc("getProjectById", project_id=self._project_id)
            if not project:
                logger.error(
                    "Kanboard project %d not found", self._project_id
                )
                return False

            columns = await self._rpc("getColumns", project_id=self._project_id)
            if columns:
                for col in columns:
                    key = col["title"].lower()
                    col_id = int(col["id"])
                    self._column_map[key] = col_id
                    status = _COLUMN_STATUS_MAP.get(key, TaskStatus.TODO)
                    self._column_status[col_id] = status

            me = await self._rpc("getMe")
            if me and "id" in me:
                self._api_user_id = int(me["id"])

            logger.info(
                "Connected to Kanboard project %d (%s), %d columns",
                self._project_id,
                project.get("name", ""),
                len(self._column_map),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Kanboard connect failed: %s", exc)
            return False

    async def disconnect(self) -> None:
        """Close the underlying HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # KanbanInterface — task retrieval
    # ------------------------------------------------------------------

    async def get_all_tasks(self) -> List[Task]:
        """Return all tasks (active + closed) for the project.

        Returns
        -------
        List[Task]
            All tasks on the board.
        """
        active = await self._rpc("getAllTasks", project_id=self._project_id, status_id=1)
        closed = await self._rpc("getAllTasks", project_id=self._project_id, status_id=0)
        raw_tasks: List[Dict[str, Any]] = []
        if active:
            raw_tasks.extend(active)
        if closed:
            raw_tasks.extend(closed)

        tasks: List[Task] = []
        for kb_task in raw_tasks:
            tasks.append(await self._kb_task_to_task(kb_task))
        return tasks

    async def get_available_tasks(self) -> List[Task]:
        """Return tasks in TODO or READY state that are unassigned.

        Returns
        -------
        List[Task]
            Tasks available for assignment.
        """
        all_tasks = await self.get_all_tasks()
        return [
            t
            for t in all_tasks
            if t.status in (TaskStatus.TODO, TaskStatus.READY)
            and t.assigned_to is None
        ]

    async def get_task_by_id(self, task_id: str) -> Optional[Task]:
        """Fetch a single task by ID.

        Parameters
        ----------
        task_id : str
            Kanboard task ID.

        Returns
        -------
        Optional[Task]
            Task object or None.
        """
        kb_task = await self._rpc("getTask", task_id=int(task_id))
        if not kb_task:
            return None
        return await self._kb_task_to_task(kb_task)

    # ------------------------------------------------------------------
    # KanbanInterface — task mutation
    # ------------------------------------------------------------------

    async def create_task(self, task_data: Dict[str, Any]) -> Task:
        """Create a new task in the project.

        Parameters
        ----------
        task_data : Dict[str, Any]
            Must contain ``name``.  Optional: ``description``, ``priority``.

        Returns
        -------
        Task
            Created task.
        """
        task_id = await self._rpc(
            "createTask",
            project_id=self._project_id,
            title=task_data.get("name", "Untitled"),
            description=task_data.get("description", ""),
            color_id="blue",
        )
        if not task_id:
            raise RuntimeError("Kanboard createTask returned no ID")
        task = await self.get_task_by_id(str(task_id))
        if task is None:
            raise RuntimeError(f"Created task {task_id} not found")
        return task

    async def update_task(self, task_id: str, updates: Dict[str, Any]) -> Optional[Task]:
        """Update fields on an existing task.

        Parameters
        ----------
        task_id : str
            Task ID.
        updates : Dict[str, Any]
            Fields to update (Kanboard field names).

        Returns
        -------
        Optional[Task]
            Updated task.
        """
        ok = await self._rpc("updateTask", id=int(task_id), **updates)
        if not ok:
            return None
        return await self.get_task_by_id(task_id)

    async def assign_task(self, task_id: str, assignee_id: str) -> bool:
        """Assign a task to a user.

        Parameters
        ----------
        task_id : str
            Task ID.
        assignee_id : str
            Kanboard user ID.

        Returns
        -------
        bool
            True on success.
        """
        result = await self._rpc(
            "updateTask", id=int(task_id), owner_id=int(assignee_id)
        )
        return bool(result)

    async def move_task_to_column(self, task_id: str, column_name: str) -> bool:
        """Move a task to a named column.

        Parameters
        ----------
        task_id : str
            Task ID.
        column_name : str
            Target column name (case-insensitive).

        Returns
        -------
        bool
            True on success.

        Raises
        ------
        ValueError
            If the column name is not found on the board.
        """
        key = column_name.lower()
        col_id = self._column_map.get(key)
        if col_id is None:
            raise ValueError(
                f"Column {column_name!r} not found. "
                f"Available: {list(self._column_map)}"
            )
        result = await self._rpc(
            "moveTaskPosition",
            project_id=self._project_id,
            task_id=int(task_id),
            column_id=col_id,
            position=1,
            swimlane_id=0,
        )
        return bool(result)

    async def add_comment(self, task_id: str, comment: str) -> bool:
        """Post a comment on a task.

        Parameters
        ----------
        task_id : str
            Task ID.
        comment : str
            Comment body (Markdown supported).

        Returns
        -------
        bool
            True on success.
        """
        result = await self._rpc(
            "createComment",
            task_id=int(task_id),
            user_id=self._api_user_id,
            content=comment,
        )
        return bool(result)

    async def report_blocker(
        self, task_id: str, blocker_description: str, severity: str = "medium"
    ) -> bool:
        """Move task to blocked column and post a comment.

        Parameters
        ----------
        task_id : str
            Task ID.
        blocker_description : str
            What is blocking the task.
        severity : str
            Not used by Kanboard; kept for interface compatibility.

        Returns
        -------
        bool
            True on success.
        """
        moved = await self.move_task_to_column(task_id, "blocked")
        commented = await self.add_comment(
            task_id, f"🚫 BLOCKED: {blocker_description}"
        )
        return moved and commented

    async def update_task_progress(
        self, task_id: str, progress_data: Dict[str, Any]
    ) -> bool:
        """Post a progress comment on a task.

        Parameters
        ----------
        task_id : str
            Task ID.
        progress_data : Dict[str, Any]
            May contain ``progress`` (int 0-100), ``message`` (str).

        Returns
        -------
        bool
            True on success.
        """
        pct = progress_data.get("progress", 0)
        msg = progress_data.get("message", "Work in progress")
        comment = f"📊 Progress: {pct}% — {msg}"
        return await self.add_comment(task_id, comment)

    async def get_project_metrics(self) -> Dict[str, Any]:
        """Return task counts per status category.

        Returns
        -------
        Dict[str, Any]
            Keys: total_tasks, backlog_tasks, in_progress_tasks,
            completed_tasks, blocked_tasks.
        """
        board = await self._rpc("getBoard", project_id=self._project_id)
        counts: Dict[str, int] = {
            "total_tasks": 0,
            "backlog_tasks": 0,
            "in_progress_tasks": 0,
            "completed_tasks": 0,
            "blocked_tasks": 0,
        }
        if not board:
            return counts

        for swimlane in board:
            for col in swimlane.get("columns", []):
                tasks = col.get("tasks", [])
                col_id = int(col.get("id", 0))
                status = self._column_status.get(col_id, TaskStatus.TODO)
                n = len(tasks)
                counts["total_tasks"] += n
                if status == TaskStatus.TODO:
                    counts["backlog_tasks"] += n
                elif status == TaskStatus.IN_PROGRESS:
                    counts["in_progress_tasks"] += n
                elif status == TaskStatus.DONE:
                    counts["completed_tasks"] += n
                elif status == TaskStatus.BLOCKED:
                    counts["blocked_tasks"] += n

        return counts

    # ------------------------------------------------------------------
    # Attachment methods (Kanboard supports file attachments via API)
    # ------------------------------------------------------------------

    async def upload_attachment(
        self,
        task_id: str,
        filename: str,
        content: Union[str, bytes],
        content_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload a file attachment to a task.

        Parameters
        ----------
        task_id : str
            Task ID.
        filename : str
            Name for the file.
        content : Union[str, bytes]
            File content as bytes or UTF-8 string.
        content_type : Optional[str]
            MIME type (unused by Kanboard API; kept for interface compat).

        Returns
        -------
        Dict[str, Any]
            ``{success, data: {id, filename}}`` or ``{success: False, error}``.
        """
        import base64

        if isinstance(content, str):
            encoded = base64.b64encode(content.encode()).decode()
        else:
            encoded = base64.b64encode(content).decode()

        result = await self._rpc(
            "createTaskFile",
            project_id=self._project_id,
            task_id=int(task_id),
            filename=filename,
            blob=encoded,
        )
        if not result:
            return {"success": False, "error": "createTaskFile returned False"}
        return {"success": True, "data": {"id": result, "filename": filename}}

    async def get_attachments(self, task_id: str) -> Dict[str, Any]:
        """List all file attachments for a task.

        Parameters
        ----------
        task_id : str
            Task ID.

        Returns
        -------
        Dict[str, Any]
            ``{success, data: [{id, filename, url}]}``.
        """
        files = await self._rpc("getAllTaskFiles", task_id=int(task_id))
        items = []
        if files:
            for f in files:
                items.append(
                    {
                        "id": str(f.get("id", "")),
                        "filename": f.get("name", ""),
                        "url": f.get("path", ""),
                        "created_at": f.get("date", ""),
                        "created_by": str(f.get("user_id", "")),
                    }
                )
        return {"success": True, "data": items}

    async def download_attachment(
        self, attachment_id: str, filename: str, task_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Download a file attachment.

        Parameters
        ----------
        attachment_id : str
            File ID returned by :meth:`get_attachments`.
        filename : str
            Expected filename.
        task_id : Optional[str]
            Task ID (unused; kept for interface compatibility).

        Returns
        -------
        Dict[str, Any]
            ``{success, data: {content, filename}}`` or ``{success: False, error}``.
        """
        import base64

        blob = await self._rpc("getTaskFile", file_id=int(attachment_id))
        if not blob:
            return {"success": False, "error": "File not found"}
        content = base64.b64decode(blob)
        return {"success": True, "data": {"content": content, "filename": filename}}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _rpc(self, method: str, **params: Any) -> Any:
        """Execute a Kanboard JSON-RPC 2.0 call.

        Parameters
        ----------
        method : str
            RPC method name.
        **params : Any
            Method parameters.

        Returns
        -------
        Any
            The ``result`` field from the RPC response.

        Raises
        ------
        RuntimeError
            On RPC-level errors.
        RuntimeError
            If the client is not initialised (connect() not called).
        """
        if self._client is None:
            raise RuntimeError("KanboardKanban not connected — call connect() first")

        payload = {"jsonrpc": "2.0", "method": method, "id": 1, "params": params}
        try:
            r = await self._client.post(self._rpc_url, json=payload, auth=self._auth)
            r.raise_for_status()
            body = r.json()
            if "error" in body:
                raise RuntimeError(f"Kanboard RPC error [{method}]: {body['error']}")
            return body.get("result")
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"Kanboard HTTP {exc.response.status_code} on {method}"
            ) from exc

    async def _kb_task_to_task(self, kb: Dict[str, Any]) -> Task:
        """Convert a raw Kanboard task dict to a Marcus Task model.

        Parameters
        ----------
        kb : Dict[str, Any]
            Raw Kanboard task dict from the API.

        Returns
        -------
        Task
            Marcus Task object.
        """
        col_id = int(kb.get("column_id", 0))
        status = self._column_status.get(col_id, TaskStatus.TODO)
        owner_id = kb.get("owner_id", "0")
        assigned_to = str(owner_id) if owner_id and str(owner_id) != "0" else None
        color = kb.get("color_id", "blue")
        priority = _COLOR_PRIORITY_MAP.get(color, Priority.MEDIUM)

        # Fetch comments and store them in source_context
        try:
            comments = await self._rpc("getAllComments", task_id=int(kb["id"])) or []
        except Exception:  # noqa: BLE001
            comments = []

        def _epoch_to_dt(val: Any) -> Optional[datetime]:
            """Convert a Kanboard Unix-epoch int to an aware datetime."""
            try:
                ts = int(val)
                if ts:
                    return datetime.fromtimestamp(ts, tz=timezone.utc)
            except (TypeError, ValueError):
                pass
            return None

        now = datetime.now(timezone.utc)
        created_at = _epoch_to_dt(kb.get("date_creation")) or now
        updated_at = _epoch_to_dt(kb.get("date_modification")) or now
        due_date = _epoch_to_dt(kb.get("date_due"))

        return Task(
            id=str(kb["id"]),
            name=kb.get("title", ""),
            description=kb.get("description", ""),
            status=status,
            priority=priority,
            assigned_to=assigned_to,
            created_at=created_at,
            updated_at=updated_at,
            due_date=due_date,
            estimated_hours=float(kb.get("time_estimated", 0.0)),
            actual_hours=float(kb.get("time_spent", 0.0)),
            labels=kb.get("tags", []) if isinstance(kb.get("tags"), list) else [],
            source_context={
                "kanboard_task": kb,
                "comments": comments,
                "column_id": col_id,
            },
        )
