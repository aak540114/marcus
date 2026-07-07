"""
Per-project and per-ticket gate-mode and AI-verify settings.

Gate mode controls whether human approval is required at key workflow
checkpoints (``human``) or whether the AI works autonomously from ready to
done without pausing for review (``ai``).

AI-verify mode (only applies when gate is ``ai``) controls whether a second
LLM pass verifies the worker agent's output before merging.  When enabled,
Marcus runs a code-review prompt against the branch diff and acceptance
criteria.  If the review finds issues it posts findings, releases the ticket
back to In Progress, and the worker agent must fix them.  Only a clean review
allows the merge to proceed.

Precedence for both settings (highest to lowest):
1. Per-ticket setting
2. Per-project setting
3. Hard default (``"human"`` for gate; ``False`` for verify)

Settings are persisted as a JSON file at::

    <data_dir>/gate_settings.json

Schema::

    {
      "projects": {"1": {"gate": "human"}, "2": {"gate": "ai", "verify": true}},
      "tickets":  {"42": {"gate": "ai"}, "99": {"gate": null, "verify": false}}
    }

A ticket entry of ``null`` for a key means "reset to project default" — the
manager stores nothing for that ticket and effective resolution falls back to
the project.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Literal, Optional

logger = logging.getLogger(__name__)

GateMode = Literal["human", "ai"]
_DEFAULT_GATE: GateMode = "human"
_DEFAULT_VERIFY: bool = False
_DEFAULT_DATA_DIR = Path(os.getcwd()) / "data"


class GateSettingManager:
    """Reads and writes per-project / per-ticket gate-mode and verify settings.

    Parameters
    ----------
    data_dir : Optional[Path]
        Directory that contains ``gate_settings.json``.  Defaults to
        ``./data/`` relative to the Marcus working directory.
    """

    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self._path = (data_dir or _DEFAULT_DATA_DIR) / "gate_settings.json"
        self._data: Dict[str, Any] = self._load()

    # ------------------------------------------------------------------
    # Gate mode — read
    # ------------------------------------------------------------------

    def get_project_gate(self, project_id: int) -> Optional[GateMode]:
        """Return the gate set for a project, or ``None`` if not set.

        Parameters
        ----------
        project_id : int
            Kanboard project ID.

        Returns
        -------
        Optional[GateMode]
            ``"human"`` or ``"ai"``, or ``None`` when no project setting
            has been stored.
        """
        val = self._project_entry(project_id).get("gate")
        return val if val in ("human", "ai") else None  # type: ignore[return-value]

    def get_ticket_gate(self, ticket_id: str) -> Optional[GateMode]:
        """Return the gate set for a specific ticket, or ``None`` if not set.

        Parameters
        ----------
        ticket_id : str
            Kanboard task ID.

        Returns
        -------
        Optional[GateMode]
            ``"human"`` or ``"ai"``, or ``None`` when the ticket inherits
            from its project.
        """
        val = self._ticket_entry(ticket_id).get("gate")
        return val if val in ("human", "ai") else None  # type: ignore[return-value]

    def get_effective_gate(self, ticket_id: str, project_id: int) -> GateMode:
        """Return the resolved gate mode for a ticket.

        Resolution order: ticket → project → ``"human"``.

        Parameters
        ----------
        ticket_id : str
            Kanboard task ID.
        project_id : int
            Kanboard project ID the ticket belongs to.

        Returns
        -------
        GateMode
            ``"human"`` or ``"ai"`` — never ``None``.
        """
        ticket_gate = self.get_ticket_gate(ticket_id)
        if ticket_gate is not None:
            return ticket_gate
        project_gate = self.get_project_gate(project_id)
        if project_gate is not None:
            return project_gate
        return _DEFAULT_GATE

    # ------------------------------------------------------------------
    # AI verify — read
    # ------------------------------------------------------------------

    def get_project_verify(self, project_id: int) -> Optional[bool]:
        """Return the verify flag for a project, or ``None`` if not set.

        Parameters
        ----------
        project_id : int
            Kanboard project ID.

        Returns
        -------
        Optional[bool]
            ``True`` / ``False``, or ``None`` when no setting has been stored.
        """
        val = self._project_entry(project_id).get("verify")
        return bool(val) if isinstance(val, bool) else None

    def get_ticket_verify(self, ticket_id: str) -> Optional[bool]:
        """Return the verify flag for a specific ticket, or ``None`` if not set.

        Parameters
        ----------
        ticket_id : str
            Kanboard task ID.

        Returns
        -------
        Optional[bool]
            ``True`` / ``False``, or ``None`` when the ticket inherits from
            its project.
        """
        val = self._ticket_entry(ticket_id).get("verify")
        return bool(val) if isinstance(val, bool) else None

    def get_effective_verify(self, ticket_id: str, project_id: int) -> bool:
        """Return the resolved AI-verify flag for a ticket.

        Resolution order: ticket → project → ``False``.

        Parameters
        ----------
        ticket_id : str
            Kanboard task ID.
        project_id : int
            Kanboard project ID the ticket belongs to.

        Returns
        -------
        bool
            ``True`` if AI verification is enabled for this ticket.
        """
        ticket_verify = self.get_ticket_verify(ticket_id)
        if ticket_verify is not None:
            return ticket_verify
        project_verify = self.get_project_verify(project_id)
        if project_verify is not None:
            return project_verify
        return _DEFAULT_VERIFY

    # ------------------------------------------------------------------
    # Gate mode — write
    # ------------------------------------------------------------------

    def set_project_gate(self, project_id: int, gate: GateMode) -> None:
        """Persist the gate mode for a project.

        Parameters
        ----------
        project_id : int
            Kanboard project ID.
        gate : GateMode
            ``"human"`` or ``"ai"``.
        """
        self._project_entry(project_id, create=True)["gate"] = gate
        self._save()
        logger.info("Set project %d gate to %r", project_id, gate)

    def set_ticket_gate(self, ticket_id: str, gate: Optional[GateMode]) -> None:
        """Persist (or clear) the gate mode for a specific ticket.

        Parameters
        ----------
        ticket_id : str
            Kanboard task ID.
        gate : Optional[GateMode]
            ``"human"`` or ``"ai"`` to override; ``None`` to reset to the
            project-level setting.
        """
        entry = self._ticket_entry(ticket_id, create=True)
        if gate is None:
            entry.pop("gate", None)
            if not entry:
                self._data.get("tickets", {}).pop(str(ticket_id), None)
        else:
            entry["gate"] = gate
        self._save()
        logger.info("Set ticket %s gate to %r", ticket_id, gate)

    # ------------------------------------------------------------------
    # AI verify — write
    # ------------------------------------------------------------------

    def set_project_verify(self, project_id: int, verify: bool) -> None:
        """Persist the AI-verify flag for a project.

        Parameters
        ----------
        project_id : int
            Kanboard project ID.
        verify : bool
            ``True`` to enable AI verification; ``False`` to disable.
        """
        self._project_entry(project_id, create=True)["verify"] = verify
        self._save()
        logger.info("Set project %d verify to %r", project_id, verify)

    def set_ticket_verify(self, ticket_id: str, verify: Optional[bool]) -> None:
        """Persist (or clear) the AI-verify flag for a specific ticket.

        Parameters
        ----------
        ticket_id : str
            Kanboard task ID.
        verify : Optional[bool]
            ``True`` / ``False`` to override; ``None`` to reset to the
            project-level setting.
        """
        entry = self._ticket_entry(ticket_id, create=True)
        if verify is None:
            entry.pop("verify", None)
            if not entry:
                self._data.get("tickets", {}).pop(str(ticket_id), None)
        else:
            entry["verify"] = verify
        self._save()
        logger.info("Set ticket %s verify to %r", ticket_id, verify)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _project_entry(self, project_id: int, *, create: bool = False) -> Dict[str, Any]:
        """Return (and optionally create) the dict for a project."""
        key = str(project_id)
        projects = self._data.setdefault("projects", {})
        if key not in projects:
            if create:
                projects[key] = {}
            else:
                return {}
        entry = projects[key]
        # Migrate old string-only format: "ai" → {"gate": "ai"}
        if isinstance(entry, str):
            projects[key] = {"gate": entry}
        return projects[key]

    def _ticket_entry(self, ticket_id: str, *, create: bool = False) -> Dict[str, Any]:
        """Return (and optionally create) the dict for a ticket."""
        key = str(ticket_id)
        tickets = self._data.setdefault("tickets", {})
        if key not in tickets:
            if create:
                tickets[key] = {}
            else:
                return {}
        entry = tickets[key]
        # Migrate old string-only format: "ai" → {"gate": "ai"}
        if isinstance(entry, str):
            tickets[key] = {"gate": entry}
        return tickets[key]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> Dict[str, Any]:
        """Load settings from disk; return an empty structure on missing file."""
        if not self._path.exists():
            return {"projects": {}, "tickets": {}}
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                return {"projects": {}, "tickets": {}}
            data.setdefault("projects", {})
            data.setdefault("tickets", {})
            return data
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read gate_settings.json: %s", exc)
            return {"projects": {}, "tickets": {}}

    def _save(self) -> None:
        """Write settings to disk."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self._path, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)
        except OSError as exc:
            logger.error("Could not write gate_settings.json: %s", exc)
