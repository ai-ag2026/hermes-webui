"""Jeder Operator-Weg, eine human-gated Karte zu lösen, muss aus dem WebUI gehen.

Vorfall 2026-08-01/02: Human-Gate-Karten (needs_input, teils vom Loop-Breaker
nach triage geparkt) waren aus dem Cockpit nicht lösbar; Gates mussten per
Chat-Formel bzw. Host-Kommando quittiert werden. Diese Tests fahren die vier
Release-Pfade gegen den ECHTEN Core (kein Fake-kb), wie
test_kanban_bridge_gated_exact_action.py.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("real_kanban_home")


@pytest.fixture
def real_kanban_home(tmp_path, monkeypatch):
    for mod in [m for m in list(sys.modules) if m == "hermes_cli" or m.startswith("hermes_cli.")]:
        del sys.modules[mod]
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    kb.init_db(home / "kanban.db")
    return kb


@pytest.fixture
def bridge(real_kanban_home, monkeypatch):
    import api.kanban_bridge as _bridge
    return importlib.reload(_bridge)


def _gated_blocked_card(kb) -> str:
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="gated block", assignee="ops")
        assert kb.claim_task(conn, task_id, claimer="w") is not None
        kb.block_task(conn, task_id, kind="needs_input", reason="config guard", human_gate=True)
        return task_id


def _gated_triage_card(kb, *, recurrences: int = 0) -> str:
    """Loop-Breaker-Endlage: human_gate=1, status=triage."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="gated triage", assignee="ops")
        conn.execute(
            "UPDATE tasks SET status='triage', human_gate=1, block_recurrences=? WHERE id=?",
            (recurrences, task_id),
        )
        conn.commit()
        return task_id


def test_unblock_releases_gated_blocked_card(bridge, real_kanban_home):
    kb = real_kanban_home
    tid = _gated_blocked_card(kb)
    out = bridge._task_action_payload(tid, {}, "unblock")
    assert out["task"]["status"] in ("ready", "todo")


def test_patch_promotes_gated_triage_card(bridge, real_kanban_home):
    kb = real_kanban_home
    tid = _gated_triage_card(kb, recurrences=17)
    out = bridge._patch_task_payload(tid, {"status": "ready"})
    assert out["task"]["status"] == "ready"


def test_patch_archives_gated_triage_card(bridge, real_kanban_home):
    kb = real_kanban_home
    tid = _gated_triage_card(kb, recurrences=17)
    out = bridge._patch_task_payload(tid, {"status": "archived"})
    assert out["task"]["status"] == "archived"


def test_patch_completes_gated_triage_card(bridge, real_kanban_home):
    kb = real_kanban_home
    tid = _gated_triage_card(kb)
    out = bridge._patch_task_payload(tid, {"status": "done"})
    assert out["task"]["status"] == "done"
