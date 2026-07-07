"""
Unit tests for src/core/gate_settings.py
"""

import json
from pathlib import Path

import pytest

from src.core.gate_settings import GateSettingManager


class TestGateSettingManager:
    """Tests for GateSettingManager — gate mode and AI-verify settings."""

    @pytest.fixture()
    def mgr(self, tmp_path: Path) -> GateSettingManager:
        """Manager backed by a temp directory."""
        return GateSettingManager(data_dir=tmp_path)

    # ── Gate defaults ────────────────────────────────────────────────────

    def test_get_project_gate_returns_none_when_not_set(self, mgr):
        """No project setting → get_project_gate returns None."""
        assert mgr.get_project_gate(1) is None

    def test_get_ticket_gate_returns_none_when_not_set(self, mgr):
        """No ticket setting → get_ticket_gate returns None."""
        assert mgr.get_ticket_gate("42") is None

    def test_get_effective_gate_defaults_to_human(self, mgr):
        """When nothing is set, effective gate is 'human'."""
        assert mgr.get_effective_gate("99", 5) == "human"

    # ── Gate — project settings ──────────────────────────────────────────

    def test_set_and_get_project_gate_human(self, mgr):
        """set_project_gate then get_project_gate round-trips 'human'."""
        mgr.set_project_gate(1, "human")
        assert mgr.get_project_gate(1) == "human"

    def test_set_and_get_project_gate_ai(self, mgr):
        """set_project_gate then get_project_gate round-trips 'ai'."""
        mgr.set_project_gate(2, "ai")
        assert mgr.get_project_gate(2) == "ai"

    def test_project_gate_persisted_to_disk(self, tmp_path):
        """set_project_gate writes data that survives a new manager instance."""
        mgr1 = GateSettingManager(data_dir=tmp_path)
        mgr1.set_project_gate(3, "ai")

        mgr2 = GateSettingManager(data_dir=tmp_path)
        assert mgr2.get_project_gate(3) == "ai"

    def test_project_gate_overrides_default(self, mgr):
        """Project 'ai' setting overrides the global 'human' default."""
        mgr.set_project_gate(1, "ai")
        assert mgr.get_effective_gate("10", 1) == "ai"

    # ── Gate — ticket settings ───────────────────────────────────────────

    def test_set_and_get_ticket_gate(self, mgr):
        """set_ticket_gate then get_ticket_gate round-trips correctly."""
        mgr.set_ticket_gate("42", "ai")
        assert mgr.get_ticket_gate("42") == "ai"

    def test_ticket_gate_overrides_project_gate(self, mgr):
        """Per-ticket 'ai' overrides project 'human' setting."""
        mgr.set_project_gate(1, "human")
        mgr.set_ticket_gate("10", "ai")
        assert mgr.get_effective_gate("10", 1) == "ai"

    def test_ticket_gate_overrides_project_gate_in_reverse(self, mgr):
        """Per-ticket 'human' overrides project 'ai' setting."""
        mgr.set_project_gate(1, "ai")
        mgr.set_ticket_gate("10", "human")
        assert mgr.get_effective_gate("10", 1) == "human"

    def test_ticket_gate_none_clears_override(self, mgr):
        """set_ticket_gate(None) removes the per-ticket override."""
        mgr.set_project_gate(1, "ai")
        mgr.set_ticket_gate("10", "human")
        mgr.set_ticket_gate("10", None)
        assert mgr.get_effective_gate("10", 1) == "ai"

    def test_ticket_gate_persisted_to_disk(self, tmp_path):
        """set_ticket_gate writes data that survives a new manager instance."""
        mgr1 = GateSettingManager(data_dir=tmp_path)
        mgr1.set_ticket_gate("55", "ai")

        mgr2 = GateSettingManager(data_dir=tmp_path)
        assert mgr2.get_ticket_gate("55") == "ai"

    # ── Gate — effective resolution precedence ───────────────────────────

    def test_effective_precedence_ticket_over_project_over_default(self, mgr):
        """Resolution: ticket → project → default ('human')."""
        mgr.set_project_gate(1, "human")
        mgr.set_ticket_gate("7", "ai")
        assert mgr.get_effective_gate("7", 1) == "ai"
        assert mgr.get_effective_gate("8", 1) == "human"
        assert mgr.get_effective_gate("9", 99) == "human"

    # ── Gate — isolation ─────────────────────────────────────────────────

    def test_settings_isolated_per_project(self, mgr):
        """Project 1 and project 2 have independent settings."""
        mgr.set_project_gate(1, "human")
        mgr.set_project_gate(2, "ai")
        assert mgr.get_project_gate(1) == "human"
        assert mgr.get_project_gate(2) == "ai"

    def test_settings_isolated_per_ticket(self, mgr):
        """Ticket 10 and ticket 20 have independent settings."""
        mgr.set_ticket_gate("10", "human")
        mgr.set_ticket_gate("20", "ai")
        assert mgr.get_ticket_gate("10") == "human"
        assert mgr.get_ticket_gate("20") == "ai"

    # ── Verify defaults ──────────────────────────────────────────────────

    def test_get_project_verify_returns_none_when_not_set(self, mgr):
        """No project verify setting → get_project_verify returns None."""
        assert mgr.get_project_verify(1) is None

    def test_get_ticket_verify_returns_none_when_not_set(self, mgr):
        """No ticket verify setting → get_ticket_verify returns None."""
        assert mgr.get_ticket_verify("42") is None

    def test_get_effective_verify_defaults_to_false(self, mgr):
        """When nothing is set, effective verify is False."""
        assert mgr.get_effective_verify("99", 5) is False

    # ── Verify — project settings ────────────────────────────────────────

    def test_set_and_get_project_verify_true(self, mgr):
        """set_project_verify(True) then get_project_verify returns True."""
        mgr.set_project_verify(1, True)
        assert mgr.get_project_verify(1) is True

    def test_set_and_get_project_verify_false(self, mgr):
        """set_project_verify(False) then get_project_verify returns False."""
        mgr.set_project_verify(1, False)
        assert mgr.get_project_verify(1) is False

    def test_project_verify_persisted_to_disk(self, tmp_path):
        """set_project_verify writes data that survives a new manager instance."""
        mgr1 = GateSettingManager(data_dir=tmp_path)
        mgr1.set_project_verify(5, True)

        mgr2 = GateSettingManager(data_dir=tmp_path)
        assert mgr2.get_project_verify(5) is True

    def test_project_verify_overrides_default_false(self, mgr):
        """Project verify=True overrides the global False default."""
        mgr.set_project_verify(1, True)
        assert mgr.get_effective_verify("10", 1) is True

    # ── Verify — ticket settings ─────────────────────────────────────────

    def test_set_and_get_ticket_verify(self, mgr):
        """set_ticket_verify then get_ticket_verify round-trips correctly."""
        mgr.set_ticket_verify("42", True)
        assert mgr.get_ticket_verify("42") is True

    def test_ticket_verify_overrides_project_verify(self, mgr):
        """Per-ticket True overrides project False."""
        mgr.set_project_verify(1, False)
        mgr.set_ticket_verify("10", True)
        assert mgr.get_effective_verify("10", 1) is True

    def test_ticket_verify_overrides_project_verify_false(self, mgr):
        """Per-ticket False overrides project True."""
        mgr.set_project_verify(1, True)
        mgr.set_ticket_verify("10", False)
        assert mgr.get_effective_verify("10", 1) is False

    def test_ticket_verify_none_clears_override(self, mgr):
        """set_ticket_verify(None) removes the per-ticket override."""
        mgr.set_project_verify(1, True)
        mgr.set_ticket_verify("10", False)
        mgr.set_ticket_verify("10", None)
        assert mgr.get_effective_verify("10", 1) is True

    def test_ticket_verify_persisted_to_disk(self, tmp_path):
        """set_ticket_verify writes data that survives a new manager instance."""
        mgr1 = GateSettingManager(data_dir=tmp_path)
        mgr1.set_ticket_verify("55", True)

        mgr2 = GateSettingManager(data_dir=tmp_path)
        assert mgr2.get_ticket_verify("55") is True

    # ── Verify — effective resolution precedence ─────────────────────────

    def test_verify_effective_precedence(self, mgr):
        """Verify resolution: ticket → project → default (False)."""
        mgr.set_project_verify(1, False)
        mgr.set_ticket_verify("7", True)
        assert mgr.get_effective_verify("7", 1) is True
        assert mgr.get_effective_verify("8", 1) is False
        assert mgr.get_effective_verify("9", 99) is False

    # ── Gate and verify coexist ──────────────────────────────────────────

    def test_gate_and_verify_stored_together(self, tmp_path):
        """Gate and verify can be set independently on the same project."""
        mgr = GateSettingManager(data_dir=tmp_path)
        mgr.set_project_gate(1, "ai")
        mgr.set_project_verify(1, True)
        assert mgr.get_project_gate(1) == "ai"
        assert mgr.get_project_verify(1) is True

    def test_gate_and_verify_independent_per_ticket(self, mgr):
        """Setting gate does not affect verify and vice versa."""
        mgr.set_ticket_gate("42", "ai")
        mgr.set_ticket_verify("42", True)
        assert mgr.get_ticket_gate("42") == "ai"
        assert mgr.get_ticket_verify("42") is True

    # ── Resilience ────────────────────────────────────────────────────────

    def test_loads_cleanly_when_file_missing(self, tmp_path):
        """No gate_settings.json on disk → manager starts with empty state."""
        mgr = GateSettingManager(data_dir=tmp_path)
        assert mgr.get_project_gate(1) is None
        assert mgr.get_ticket_gate("1") is None
        assert mgr.get_project_verify(1) is None

    def test_survives_corrupt_json(self, tmp_path):
        """Corrupt JSON file → manager falls back to empty state."""
        (tmp_path / "gate_settings.json").write_text("NOT JSON {{{{")
        mgr = GateSettingManager(data_dir=tmp_path)
        assert mgr.get_effective_gate("1", 1) == "human"
        assert mgr.get_effective_verify("1", 1) is False

    def test_json_file_structure(self, tmp_path):
        """Saved JSON has 'projects' and 'tickets' keys with nested dicts."""
        mgr = GateSettingManager(data_dir=tmp_path)
        mgr.set_project_gate(1, "ai")
        mgr.set_project_verify(1, True)
        raw = json.loads((tmp_path / "gate_settings.json").read_text())
        assert "projects" in raw
        assert "tickets" in raw
        assert raw["projects"]["1"]["gate"] == "ai"
        assert raw["projects"]["1"]["verify"] is True

    def test_migrates_old_string_format(self, tmp_path):
        """Old format (string value) is transparently migrated on first access."""
        data = {"projects": {"1": "ai"}, "tickets": {"42": "human"}}
        (tmp_path / "gate_settings.json").write_text(json.dumps(data))
        mgr = GateSettingManager(data_dir=tmp_path)
        assert mgr.get_project_gate(1) == "ai"
        assert mgr.get_ticket_gate("42") == "human"
