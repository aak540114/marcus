"""
Per-project and per-ticket gate-mode settings.

Gate mode controls whether human approval is required at key workflow
checkpoints (``human``) or whether the AI works autonomously from ready to
done without pausing for review (``ai``).

Precedence (highest to lowest):
1. Per-ticket setting (``set_ticket_gate``)
2. Per-project setting (``set_project_gate``)
3. Hard default: ``"human"``

Settings are persisted as a JSON file at::

    <data_dir>/gate_settings.json

Schema::

    {
      "projects": {"1": "human", "2": "ai"},
      "tickets":  {"42": "ai"}
    }

A ticket entry of ``null`` means "reset to project default" — the manager
stores nothing for that ticket and effective resolution falls back to the
project.
"""

import json
import logging
import os
from pathlib import Path
from typing import Dict, Literal, Optional

logger = logging.getLogger(__name__)

GateMode = Literal["human", "ai"]
_DEFAULT_GATE: GateMode = "human"
_DEFAULT_DATA_DIR = Path(os.getcwd()) / "data"


class GateSettingManager:
    """Reads and writes per-project / per-ticket gate-mode settings.

    Parameters
    ----------
    data_dir : Optional[Path]
        Directory that contains ``gate_settings.json``.  Defaults to
        ``./data/`` relative to the Marcus working directory.
    """

    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self._path = (data_dir or _DEFAULT_DATA_DIR) / "gate_settings.json"
        self._data: Dict[str, Dict[str, str]] = self._load()

    # ------------------------------------------------------------------
    # Read
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
            has been stored (caller should fall back to the default).
        """
        val = self._data.get("projects", {}).get(str(project_id))
        return val if val in ("human", "ai") else None  # type: ignore[return-value]

    def get_ticket_gate(self, ticket_id: str) -> Optional[GateMode]:
        """Return the gate set for a specific ticket, or ``None`` if not set.

        Parameters
        ----------
        ticket_id : str
            Kanboard task ID (as a string).

        Returns
        -------
        Optional[GateMode]
            ``"human"`` or ``"ai"``, or ``None`` when the ticket inherits
            from its project.
        """
        val = self._data.get("tickets", {}).get(str(ticket_id))
        return val if val in ("human", "ai") else None  # type: ignore[return-value]

    def get_effective_gate(self, ticket_id: str, project_id: int) -> GateMode:
        """Return the resolved gate mode for a ticket.

        Resolution order:
        1. Per-ticket setting
        2. Per-project setting
        3. Hard default (``"human"``)

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
    # Write
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
        self._data.setdefault("projects", {})[str(project_id)] = gate
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
        tickets = self._data.setdefault("tickets", {})
        if gate is None:
            tickets.pop(str(ticket_id), None)
        else:
            tickets[str(ticket_id)] = gate
        self._save()
        logger.info("Set ticket %s gate to %r", ticket_id, gate)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> Dict[str, Dict[str, str]]:
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
