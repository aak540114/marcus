"""
Human-gated AI workflow orchestrator.

This module ties together the board watcher, ticket lifecycle manager,
acceptance criteria engine, git branch manager, comment protocol, and
dev environment manager into the end-to-end workflow described below.

Full lifecycle
--------------
1. ``BoardWatcher`` detects a new (or existing) ticket.
2. If no Marcus AC block exists, ``ACGenerator`` produces one and posts
   it as a comment.  The AC is also embedded in the ticket description.
3. The board watcher polls until a human assigns the ticket to themselves.
4. On assignment, a ``ticket/{provider}/{id}`` branch is created and the
   AI agent is notified to start work (via a Marcus comment on the ticket).
5. The AI agent works, posting periodic progress comments.
6. When the AI agent signals completion, the ticket moves to
   ``AWAITING_ACCEPTANCE`` and a "Ready for Review" comment is posted.
7. If the human comments with feedback, the AI re-reads the AC and
   continues on the same branch, posting a revision acknowledgement.
8. When the human closes / transitions the ticket to Done, the branch is
   merged to main, a "Merged" comment is posted, and the ticket moves to
   ``ACCEPTED``.
9. If the human later reopens the ticket, the branch is rebased on main
   and work resumes from step 5.

Hot-reload preview
------------------
At any point a human can comment ``@marcus start-dev-env`` on the ticket
(or click a button in a future UI) to spin up a hot-reload dev
environment on the ticket branch.  The URL is posted back as a comment.

Classes
-------
HumanGatedWorkflow
    Central orchestrator.  Subscribe to the Marcus ``Events`` bus to
    receive board events, then call :meth:`handle_event` to route them.
"""

import logging
from typing import Any, List, Optional

from src.core.acceptance_criteria import ACChangeDetector, ACGenerator, ACParser
from src.core.board_watcher import BoardWatcher
from src.core.comment_protocol import CommentFormatter, CommentParser
from src.core.dev_environment import DevEnvironmentManager
from src.core.events import Events
from src.core.git_branch_manager import BranchManager
from src.core.ticket_lifecycle import (
    InvalidTransitionError,
    TicketLifecycleManager,
    TicketRecord,
    TicketState,
)
from src.integrations.kanban_interface import KanbanInterface

logger = logging.getLogger(__name__)


class HumanGatedWorkflow:
    """Orchestrates the human-approval workflow for every ticket.

    Parameters
    ----------
    kanban : KanbanInterface
        Connected kanban provider.
    events : Events
        Shared Marcus event bus.
    provider_name : str
        Short label for the provider (``"github"``, ``"jira"``, etc.).
    lifecycle : Optional[TicketLifecycleManager]
        Lifecycle state store.  Created with defaults if not provided.
    branch_manager : Optional[BranchManager]
        Git branch manager.  Created with defaults if not provided.
    dev_env_manager : Optional[DevEnvironmentManager]
        Dev environment manager.  Created with defaults if not provided.
    ac_generator : Optional[ACGenerator]
        AC generator (may have an injected LLM callable).
    poll_interval : float
        Seconds between board polls for the ``BoardWatcher``.
    """

    def __init__(
        self,
        kanban: KanbanInterface,
        events: Events,
        provider_name: str,
        lifecycle: Optional[TicketLifecycleManager] = None,
        branch_manager: Optional[BranchManager] = None,
        dev_env_manager: Optional[DevEnvironmentManager] = None,
        ac_generator: Optional[ACGenerator] = None,
        poll_interval: float = 30.0,
    ) -> None:
        """Initialise the workflow."""
        self._kanban = kanban
        self._events = events
        self._provider = provider_name
        self._lifecycle = lifecycle or TicketLifecycleManager()
        self._branch = branch_manager or BranchManager()
        self._dev_env = dev_env_manager or DevEnvironmentManager()
        self._ac_gen = ac_generator or ACGenerator()
        self._watcher = BoardWatcher(
            kanban=kanban,
            events=events,
            provider_name=provider_name,
            poll_interval=poll_interval,
            on_error=self._on_watcher_error,
        )
        self._subscribed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Subscribe to events and start polling."""
        if not self._subscribed:
            self._subscribe_events()
            self._subscribed = True
        await self._watcher.start()
        logger.info("HumanGatedWorkflow started for provider=%s", self._provider)

    async def stop(self) -> None:
        """Stop polling and shut down all dev environments."""
        await self._watcher.stop()
        await self._dev_env.stop_all()
        logger.info("HumanGatedWorkflow stopped for provider=%s", self._provider)

    # ------------------------------------------------------------------
    # Event subscriptions
    # ------------------------------------------------------------------

    def _subscribe_events(self) -> None:
        """Wire board watcher events to handler methods."""
        self._events.subscribe("ticket.new", self._on_ticket_new)
        self._events.subscribe("ticket.assigned", self._on_ticket_assigned)
        self._events.subscribe("ticket.unassigned", self._on_ticket_unassigned)
        self._events.subscribe("ticket.closed", self._on_ticket_closed)
        self._events.subscribe("ticket.reopened", self._on_ticket_reopened)
        self._events.subscribe("ticket.comment_added", self._on_comment_added)
        self._events.subscribe("ticket.ac_changed", self._on_ac_changed)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_ticket_new(self, event: Any) -> None:
        """Handle a ticket seen for the first time."""
        data = event.data
        ticket_id = data["ticket_id"]
        task = data.get("task", {})
        description = task.get("description", "")
        title = task.get("title", ticket_id)

        record = self._lifecycle.get_or_create(ticket_id, self._provider)

        # If there's no Marcus AC block yet, generate one.
        existing_ac = ACParser.extract(description)
        if existing_ac is None:
            await self._generate_and_post_ac(
                ticket_id=ticket_id,
                title=title,
                description=description,
                was_human_created=True,
                record=record,
            )
        else:
            # AC already present (AI-created ticket) — just store the hash.
            if not record.ac_hash:
                new_hash = ACChangeDetector.hash_ac(existing_ac.raw_text)
                self._lifecycle.update_acceptance_criteria(
                    ticket_id, self._provider, existing_ac.raw_text, new_hash
                )

    async def _on_ticket_assigned(self, event: Any) -> None:
        """Handle a ticket being assigned to a human.

        AI work only starts here — unassigned tickets are ignored.
        """
        data = event.data
        ticket_id = data["ticket_id"]
        assignee = data.get("assignee", "unknown")

        record = self._lifecycle.get_or_create(ticket_id, self._provider)

        if record.state not in (TicketState.UNASSIGNED, TicketState.ASSIGNED):
            logger.debug(
                "Ticket %s already in state %s — ignoring re-assign event",
                ticket_id,
                record.state.value,
            )
            return

        # Transition to ASSIGNED.
        try:
            self._lifecycle.transition(
                ticket_id,
                self._provider,
                TicketState.ASSIGNED,
                reason=f"Human {assignee!r} assigned ticket to themselves",
                assignee=assignee,
            )
        except InvalidTransitionError as exc:
            logger.debug("Cannot transition to ASSIGNED: %s", exc)
            return

        # Create the ticket branch.
        branch_name = record.branch_name or BranchManager.make_branch_name(
            self._provider, ticket_id
        )
        created = await self._branch.create_branch(branch_name)
        if not created:
            logger.error(
                "Failed to create branch %s for ticket %s", branch_name, ticket_id
            )
            await self._post_error(
                ticket_id,
                f"Failed to create git branch `{branch_name}`. "
                "Please check repository permissions.",
            )
            return

        # Transition to IN_PROGRESS and post "started" comment.
        self._lifecycle.transition(
            ticket_id,
            self._provider,
            TicketState.IN_PROGRESS,
            reason="Branch created; AI agent beginning work",
        )

        ac_items = self._get_ac_items(record)
        comment = CommentFormatter.started(
            ticket_id=ticket_id,
            branch_name=branch_name,
            assignee=assignee,
            ac_items=ac_items,
        )
        await self._post_comment(ticket_id, comment)
        logger.info("AI work started for ticket %s (branch %s)", ticket_id, branch_name)

    async def _on_ticket_unassigned(self, event: Any) -> None:
        """Handle a ticket being unassigned (AI should pause/stop)."""
        data = event.data
        ticket_id = data["ticket_id"]
        record = self._lifecycle.get(ticket_id, self._provider)
        if record and record.state == TicketState.IN_PROGRESS:
            try:
                self._lifecycle.transition(
                    ticket_id,
                    self._provider,
                    TicketState.UNASSIGNED,
                    reason="Ticket unassigned by human",
                )
            except InvalidTransitionError:
                pass

    async def _on_ticket_closed(self, event: Any) -> None:
        """Handle a ticket being closed — merge branch to main."""
        data = event.data
        ticket_id = data["ticket_id"]
        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None:
            return

        if record.state not in (
            TicketState.IN_PROGRESS,
            TicketState.AWAITING_ACCEPTANCE,
            TicketState.REVISION_REQUESTED,
        ):
            return

        branch_name = record.branch_name
        main_branch = self._branch.config.main_branch

        merge_msg = (
            f"merge: ticket/{self._provider}/{ticket_id}"
            f" (accepted by {record.assignee})"
        )
        merged = await self._branch.merge_to_main(
            branch_name,
            commit_message=merge_msg,
        )

        if merged:
            self._lifecycle.transition(
                ticket_id,
                self._provider,
                TicketState.ACCEPTED,
                reason="Human accepted; branch merged to main",
            )
            self._lifecycle.set_merged(ticket_id, self._provider)

            # Stop dev env if running.
            await self._dev_env.stop(ticket_id, self._provider)

            comment = CommentFormatter.merged(
                ticket_id=ticket_id,
                branch_name=branch_name,
                main_branch=main_branch,
            )
            await self._post_comment(ticket_id, comment)
            logger.info("Ticket %s accepted and merged to %s", ticket_id, main_branch)
        else:
            await self._post_error(
                ticket_id,
                f"Merge of `{branch_name}` to `{main_branch}` failed — "
                "there may be conflicts.  Please merge manually or rebase the branch.",
            )

    async def _on_ticket_reopened(self, event: Any) -> None:
        """Handle a ticket being reopened — rebase branch on main and resume."""
        data = event.data
        ticket_id = data["ticket_id"]
        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None:
            return

        branch_name = record.branch_name

        rebased = await self._branch.rebase_on_main(branch_name)
        if not rebased:
            await self._post_error(
                ticket_id,
                f"Rebase of `{branch_name}` on `{self._branch.config.main_branch}` "
                "failed — please resolve conflicts manually.",
            )
            return

        try:
            self._lifecycle.transition(
                ticket_id,
                self._provider,
                TicketState.REOPENED,
                reason="Human reopened ticket",
            )
            self._lifecycle.transition(
                ticket_id,
                self._provider,
                TicketState.IN_PROGRESS,
                reason="Branch rebased on main; AI resuming work",
            )
        except InvalidTransitionError as exc:
            logger.debug("State transition on reopen failed: %s", exc)

        logger.info(
            "Ticket %s reopened; branch %s rebased on main", ticket_id, branch_name
        )

    async def _on_comment_added(self, event: Any) -> None:
        """Handle a new human comment on a ticket."""
        data = event.data
        ticket_id = data["ticket_id"]
        body = data.get("comment_body", "")
        author = data.get("comment_author", "")

        # Ignore Marcus's own comments.
        if CommentParser.is_marcus_comment(body):
            return

        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None or record.state == TicketState.UNASSIGNED:
            return

        # Check for @marcus commands.
        if CommentParser.contains_command(body, "start-dev-env"):
            await self._handle_start_dev_env_command(ticket_id, record)
            return

        # If AI is awaiting acceptance, treat any human comment as a
        # revision request.
        if record.state == TicketState.AWAITING_ACCEPTANCE:
            try:
                self._lifecycle.transition(
                    ticket_id,
                    self._provider,
                    TicketState.REVISION_REQUESTED,
                    reason=f"Human {author!r} requested revision",
                )
            except InvalidTransitionError:
                pass

            # Acknowledge the revision request.
            comment = CommentFormatter.revision_requested(
                ticket_id=ticket_id,
                human_comment=body,
                ai_understanding=(
                    "I'll re-read the acceptance criteria and your latest "
                    "comment, apply the requested changes, and post a new "
                    "Ready for Review update when complete."
                ),
            )
            await self._post_comment(ticket_id, comment)

        elif record.state == TicketState.REVISION_REQUESTED:
            # Human adding more detail while AI is already reworking.
            logger.debug(
                "Additional comment on %s during revision: %s", ticket_id, body[:100]
            )

    async def _on_ac_changed(self, event: Any) -> None:
        """Handle human edits to the acceptance criteria."""
        data = event.data
        ticket_id = data["ticket_id"]
        new_ac = data.get("new_ac_text", "")
        new_hash = data.get("new_hash", "")

        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None:
            return

        self._lifecycle.update_acceptance_criteria(
            ticket_id, self._provider, new_ac, new_hash
        )

        if record.state in (TicketState.IN_PROGRESS, TicketState.AWAITING_ACCEPTANCE):
            try:
                self._lifecycle.transition(
                    ticket_id,
                    self._provider,
                    TicketState.REVISION_REQUESTED,
                    reason="Acceptance criteria edited by human",
                )
            except InvalidTransitionError:
                pass

            comment = CommentFormatter.revision_requested(
                ticket_id=ticket_id,
                human_comment="*(acceptance criteria edited in ticket description)*",
                ai_understanding=(
                    "The acceptance criteria have been updated.  I'll re-read "
                    "them now and adjust the implementation accordingly."
                ),
            )
            await self._post_comment(ticket_id, comment)
            logger.info("AC change detected on ticket %s — notified agent", ticket_id)

    # ------------------------------------------------------------------
    # Agent-facing helpers (called by MCP tools)
    # ------------------------------------------------------------------

    async def report_progress(
        self,
        ticket_id: str,
        percentage: int,
        message: str,
    ) -> bool:
        """Post a progress comment on behalf of the AI agent.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.
        percentage : int
            Completion percentage (0–100).
        message : str
            Progress description.

        Returns
        -------
        bool
            ``True`` if the comment was posted successfully.
        """
        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None:
            return False

        commits = await self._branch.get_branch_commits(record.branch_name)
        comment = CommentFormatter.progress(
            ticket_id=ticket_id,
            branch_name=record.branch_name,
            percentage=percentage,
            message=message,
            commits=commits,
        )
        return await self._post_comment(ticket_id, comment)

    async def signal_ready_for_review(self, ticket_id: str) -> bool:
        """Signal that the AI agent is done and awaiting human acceptance.

        Posts a "Ready for Review" comment and transitions the ticket to
        ``AWAITING_ACCEPTANCE``.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.

        Returns
        -------
        bool
            ``True`` on success.
        """
        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None:
            return False

        try:
            self._lifecycle.transition(
                ticket_id,
                self._provider,
                TicketState.AWAITING_ACCEPTANCE,
                reason="AI agent signalled implementation complete",
            )
        except InvalidTransitionError as exc:
            logger.error("Cannot move %s to AWAITING_ACCEPTANCE: %s", ticket_id, exc)
            return False

        dev_info = self._dev_env.get_info(ticket_id, self._provider)
        dev_url = dev_info.url if dev_info else None

        commits = await self._branch.get_branch_commits(record.branch_name)
        ac_items = self._get_ac_items(record)
        comment = CommentFormatter.ready_for_review(
            ticket_id=ticket_id,
            branch_name=record.branch_name,
            ac_items=ac_items,
            dev_env_url=dev_url,
            commit_count=len(commits),
        )
        return await self._post_comment(ticket_id, comment)

    async def start_dev_environment(self, ticket_id: str) -> Optional[str]:
        """Spin up the hot-reload dev environment for a ticket branch.

        Parameters
        ----------
        ticket_id : str
            Ticket identifier.

        Returns
        -------
        Optional[str]
            URL of the running environment, or ``None`` on failure.
        """
        record = self._lifecycle.get(ticket_id, self._provider)
        if record is None:
            return None

        try:
            info = await self._dev_env.start(
                ticket_id=ticket_id,
                provider=self._provider,
                branch_name=record.branch_name,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to start dev env for %s: %s", ticket_id, exc)
            return None

        # Store the port in lifecycle record.
        self._lifecycle.set_dev_env_port(ticket_id, self._provider, info.port)

        # Post a comment with the URL.
        comment = CommentFormatter.dev_env_started(
            ticket_id=ticket_id,
            branch_name=record.branch_name,
            url=info.url,
            port=info.port,
        )
        await self._post_comment(ticket_id, comment)
        return info.url

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _generate_and_post_ac(
        self,
        ticket_id: str,
        title: str,
        description: str,
        was_human_created: bool,
        record: TicketRecord,
    ) -> None:
        """Generate AC via LLM/heuristic and post it on the ticket."""
        ac_markdown = await self._ac_gen.generate(
            title=title,
            description=description,
        )
        comment = CommentFormatter.ac_generated(
            ticket_id=ticket_id,
            ac_markdown=ac_markdown,
            was_human_created=was_human_created,
        )
        await self._post_comment(ticket_id, comment)

        # Embed the AC block in the ticket description.
        from src.core.acceptance_criteria import ACParser

        new_desc = ACParser.embed(description, ac_markdown)
        try:
            await self._kanban.update_task(ticket_id, {"description": new_desc})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not embed AC in ticket description: %s", exc)

        # Store hash in lifecycle record.
        import hashlib

        new_hash = hashlib.sha256(ac_markdown.encode()).hexdigest()
        self._lifecycle.update_acceptance_criteria(
            ticket_id, self._provider, ac_markdown, new_hash
        )

    async def _handle_start_dev_env_command(
        self, ticket_id: str, record: TicketRecord
    ) -> None:
        """Handle the ``@marcus start-dev-env`` comment command."""
        url = await self.start_dev_environment(ticket_id)
        if url is None:
            await self._post_error(
                ticket_id,
                "Failed to start dev environment.  "
                "Check that Docker is running and the repository is accessible.",
            )

    def _get_ac_items(self, record: TicketRecord) -> List[str]:
        """Return the list of AC item texts from the stored AC markdown."""
        from src.core.acceptance_criteria import ACParser

        if not record.acceptance_criteria:
            return []
        ac = ACParser.extract(
            f"<!-- MARCUS_AC_START -->\n## Acceptance Criteria\n\n"
            f"{record.acceptance_criteria}\n<!-- MARCUS_AC_END -->"
        )
        if ac is None:
            # The stored text might not have sentinels — try parsing directly.
            import re

            items = re.findall(
                r"^- \[[ xX]\] (.+)$", record.acceptance_criteria, re.MULTILINE
            )
            return items
        return [item.text for item in ac.items]

    async def _post_comment(self, ticket_id: str, body: str) -> bool:
        """Post a comment via the kanban provider (best-effort)."""
        try:
            result = await self._kanban.add_comment(ticket_id, body)
            return bool(result)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to post comment on %s: %s", ticket_id, exc)
            return False

    async def _post_error(self, ticket_id: str, error_summary: str) -> None:
        """Post an error comment on a ticket."""
        comment = CommentFormatter.error(
            ticket_id=ticket_id, error_summary=error_summary
        )
        await self._post_comment(ticket_id, comment)

    async def _on_watcher_error(self, exc: Exception) -> None:
        """Handle a poll cycle failure reported by the BoardWatcher."""
        logger.error("Board watcher error in HumanGatedWorkflow: %s", exc)
