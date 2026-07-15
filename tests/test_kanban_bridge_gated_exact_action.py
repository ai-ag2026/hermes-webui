"""Regression (2026-07-15): approving an exact terminal action on a HUMAN-GATED
card must work from the WebUI.

Field failure: card `Audit 2026-07-15 A` (human_gate=1) hit an exact terminal
action. The cockpit's only two buttons both refused, deadlocking the card:

  * /approve-exact-action -> the Core also unblocks the card as part of the
    approval, so it enforces the human gate. The bridge passed no token, the
    Core raised GateTokenError, the versioned seam mapped that to `conflict`,
    the legacy bool wrapper turned it into False, and the operator saw
    "exact terminal action approval conflicted with newer state" -- a race that
    never happened.
  * /unblock -> refuses while an exact action is pending ("approve that exact
    action first").

The existing bridge tests all run against the in-file fake `kb`, whose FakeTask
has no `human_gate` field at all, so no test ever exercised the gate. These
tests therefore drive the REAL hermes_cli.kanban_db against a throwaway board.

NOTE: HERMES_HOME must stay off ~/.hermes -- a board rooted there writes into
the live kanban DB (get_default_hermes_root mapping).
"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("real_kanban_home")


@pytest.fixture
def real_kanban_home(tmp_path, monkeypatch):
    """Point the real Core + bridge at a throwaway board under pytest's tmp_path."""
    # Drop any fake hermes_cli a sibling test module may have installed.
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
def bridge(real_kanban_home, monkeypatch):
    import api.kanban_bridge as _bridge

    return importlib.reload(_bridge)


def _gated_card_with_pending_exact_action(kb, conn, *, gated: bool):
    task_id = kb.create_task(conn, title="Audit A", assignee="analyst")
    kb.claim_task(conn, task_id)
    run_id = kb.get_task(conn, task_id).current_run_id
    kb.record_pending_action_and_block(
        conn, task_id=task_id, run_id=run_id, command="python -c 'print(1)'",
        summary="an exact terminal action", profile="analyst", workspace="/tmp",
        expires_at=int(time.time()) + 3600,
    )
    if gated:
        kb.set_human_gate(conn, task_id, on=True, actor="test")
    task = kb.get_task(conn, task_id)
    assert task.status == "blocked"
    assert bool(task.human_gate) is gated
    action = kb.get_pending_action(conn, task_id)
    return task_id, action.id


def test_gated_card_exact_action_approval_succeeds(real_kanban_home, bridge):
    """The regression: this raised 'conflicted with newer state' before the fix."""
    kb = real_kanban_home
    with kb.connect() as conn:
        task_id, action_id = _gated_card_with_pending_exact_action(kb, conn, gated=True)

    result = bridge._approve_exact_action_payload(task_id, {"pending_action_id": action_id})

    assert result["ok"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task.status in ("ready", "todo"), "gated card must leave blocked on approval"
        action = kb.get_pending_action_by_id(conn, task_id, action_id)
        assert action.state == "approved"
        assert action.approved_at is not None


def test_ungated_card_exact_action_approval_still_succeeds(real_kanban_home, bridge):
    """Behavioural neutrality: the ungated path mints no token and is unchanged."""
    kb = real_kanban_home
    with kb.connect() as conn:
        task_id, action_id = _gated_card_with_pending_exact_action(kb, conn, gated=False)

    result = bridge._approve_exact_action_payload(task_id, {"pending_action_id": action_id})

    assert result["ok"] is True
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status in ("ready", "todo")
        assert kb.get_pending_action_by_id(conn, task_id, action_id).state == "approved"
        # No gate -> no token was ever minted for this card.
        row = conn.execute(
            "SELECT gate_token_hash FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        assert row["gate_token_hash"] is None


def test_gated_approval_consumes_its_token_and_does_not_leave_a_reusable_grant(
    real_kanban_home, bridge
):
    """The minted token is single-use: it must not linger as a replayable grant."""
    kb = real_kanban_home
    with kb.connect() as conn:
        task_id, action_id = _gated_card_with_pending_exact_action(kb, conn, gated=True)

    assert bridge._approve_exact_action_payload(task_id, {"pending_action_id": action_id})["ok"]

    with kb.connect() as conn:
        # Gate flag persists (it re-arms on the next block); the grant must not.
        assert bool(kb.get_task(conn, task_id).human_gate) is True
        row = conn.execute(
            "SELECT gate_token_hash FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        assert row["gate_token_hash"] is None, "one-shot grant must be consumed, not left open"
