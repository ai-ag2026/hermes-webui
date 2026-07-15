"""Regression (2026-07-15): the WebUI must not let a raw status write walk a
HUMAN-GATED card out of blocked/scheduled.

Found while fixing the exact-action approval deadlock. The Core guards every
exit out of blocked/scheduled on a gated card (_assert_human_gate_open), but
only inside its structured verbs. `_patch_task` routes `todo`/`triage` -- and
`ready` from `scheduled` -- to `_set_status_direct`, which is raw SQL and never
consults the gate.

Verified escalation before the fix:
    blocked + human_gate=1  --PATCH status=todo-->  todo   (no token, no trail)
    recompute_ready()       -------------------->  ready
    ready                   -------------------->  dispatcher claims it, worker runs

That is a full bypass of the defence the 2026-07-13 incident hardened: a card a
human was supposed to release could be executed without any human ever holding a
token. The gated `ready`-from-`blocked` path was already refused by unblock_task;
`todo`/`triage`/`scheduled->ready` were not.

NOTE: HERMES_HOME must stay off ~/.hermes -- a board rooted there writes into
the live kanban DB (get_default_hermes_root mapping).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture
def kb_real(tmp_path, monkeypatch):
    for mod in ("hermes_cli", "hermes_cli.kanban_db"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb

    kb.init_db()
    assert str(kb.kanban_db_path()).startswith(str(tmp_path)), "must not touch the live board"
    return kb


@pytest.fixture
def bridge(kb_real, monkeypatch):
    import api.kanban_bridge as _bridge

    return importlib.reload(_bridge)


def _blocked_card(kb, conn, *, gated: bool):
    task_id = kb.create_task(conn, title="card", assignee="w")
    kb.claim_task(conn, task_id)
    kb.block_task(conn, task_id, kind="needs_input", reason="needs a human", human_gate=gated)
    assert kb.get_task(conn, task_id).status == "blocked"
    return task_id


@pytest.mark.parametrize("target", ["todo", "triage", "ready"])
def test_gated_blocked_card_cannot_be_moved_by_a_direct_status_write(kb_real, bridge, target):
    kb = kb_real
    with kb.connect() as conn:
        task_id = _blocked_card(kb, conn, gated=True)

        with pytest.raises((RuntimeError, kb.GateTokenError)):
            bridge._patch_task(conn, task_id, {"status": target})

        assert kb.get_task(conn, task_id).status == "blocked", "gate must hold"


def test_gated_scheduled_card_cannot_be_moved_to_ready(kb_real, bridge):
    """`scheduled` is gated exactly like `blocked` -- the Core says so, and the
    bridge's old `status == "blocked"` check missed it."""
    kb = kb_real
    with kb.connect() as conn:
        task_id = _blocked_card(kb, conn, gated=True)
        conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (task_id,))
        conn.commit()

        with pytest.raises((RuntimeError, kb.GateTokenError)):
            bridge._patch_task(conn, task_id, {"status": "ready"})

        assert kb.get_task(conn, task_id).status == "scheduled"


def test_bypass_cannot_reach_the_dispatcher(kb_real, bridge):
    """The full escalation chain, end to end: the card must never become ready."""
    kb = kb_real
    with kb.connect() as conn:
        task_id = _blocked_card(kb, conn, gated=True)

        with pytest.raises((RuntimeError, kb.GateTokenError)):
            bridge._patch_task(conn, task_id, {"status": "todo"})
        kb.recompute_ready(conn)

        assert kb.get_task(conn, task_id).status == "blocked", (
            "a gated card must never reach 'ready', where the dispatcher would claim it"
        )


@pytest.mark.parametrize("target", ["todo", "triage", "ready"])
def test_ungated_blocked_card_still_moves_freely(kb_real, bridge, target):
    """Behavioural neutrality: the guard keys on human_gate, nothing else."""
    kb = kb_real
    with kb.connect() as conn:
        task_id = _blocked_card(kb, conn, gated=False)

        bridge._patch_task(conn, task_id, {"status": target})

        assert kb.get_task(conn, task_id).status == target


def test_unblock_button_still_releases_a_gated_card(kb_real, bridge):
    """The supported release path must stay open -- otherwise the fix would just
    trade a bypass for a deadlock."""
    kb = kb_real
    with kb.connect() as conn:
        task_id = _blocked_card(kb, conn, gated=True)
        task = kb.get_task(conn, task_id)

        assert bridge._unblock_gate_aware(conn, task_id, task, "operator releases it")

        assert kb.get_task(conn, task_id).status in ("ready", "todo")
