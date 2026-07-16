"""WS1 (2026-07-15): an operator must be able to REJECT an exact terminal action.

The incident's real lesson. Card `Audit 2026-07-15 A` raised an exact action that
the analyst which raised it then declared unnecessary, recommending explicitly:
"Operator muss die unnötige pending Action ablehnen/canceln". There was no way to
do that -- the cockpit only ever offered "approve", the bridge had no reject
route at all, and /unblock refuses while an action is pending. The rejection had
to be done with a direct DB script.

Rejection is not approval-by-another-name:
  * it never executes the command,
  * it needs no gate token (the gate guards exits from `blocked`; this isn't one),
  * and it leaves the card `blocked` on purpose -- releasing it is a separate,
    conscious act.

NOTE: HERMES_HOME must stay off ~/.hermes -- a board rooted there writes into
the live kanban DB (get_default_hermes_root mapping).
"""

from __future__ import annotations

import importlib
import sys
import time
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


def _card_with_pending_exact_action(kb, conn, *, gated: bool = False):
    task_id = kb.create_task(conn, title="danger", assignee="worker")
    kb.claim_task(conn, task_id)
    run_id = kb.get_task(conn, task_id).current_run_id
    kb.record_pending_action_and_block(
        conn, task_id=task_id, run_id=run_id, command="git push origin main",
        summary="push to prod", profile="worker", workspace="/tmp",
        expires_at=int(time.time()) + 3600,
    )
    if gated:
        kb.set_human_gate(conn, task_id, on=True, actor="test")
    action = kb.get_pending_action(conn, task_id)
    return task_id, action.id


def test_reject_settles_the_action_and_keeps_the_card_blocked(kb_real, bridge):
    """The exact scenario from 2026-07-15."""
    kb = kb_real
    with kb.connect() as conn:
        task_id, action_id = _card_with_pending_exact_action(kb, conn)

    result = bridge._reject_exact_action_payload(
        task_id, {"pending_action_id": action_id, "reason": "auditinduziert, unnötig"}
    )

    assert result["ok"] is True
    with kb.connect() as conn:
        assert kb.get_pending_action_by_id(conn, task_id, action_id).state == "resolved"
        assert kb.get_pending_action(conn, task_id) is None, "no live action may remain"
        assert kb.get_task(conn, task_id).status == "blocked", (
            "rejecting must not release the card -- that is a separate decision"
        )
        # The command must never have run: approval is the only path that grants it.
        assert kb.get_pending_action_by_id(conn, task_id, action_id).approved_at is None


def test_reject_works_on_a_gated_card_without_any_token(kb_real):
    """Rejection is not an exit from `blocked`, so the gate must not block it.

    This is the whole reason reject is the safe escape from the deadlock: the
    approve path needs a gate token, this one needs nothing.
    """
    kb = kb_real
    import importlib

    import api.kanban_bridge as _bridge
    bridge = importlib.reload(_bridge)
    with kb.connect() as conn:
        task_id, action_id = _card_with_pending_exact_action(kb, conn, gated=True)
        assert bool(kb.get_task(conn, task_id).human_gate) is True

    result = bridge._reject_exact_action_payload(
        task_id, {"pending_action_id": action_id, "reason": "nicht gewollt"}
    )

    assert result["ok"] is True
    with kb.connect() as conn:
        assert kb.get_pending_action_by_id(conn, task_id, action_id).state == "resolved"
        assert kb.get_task(conn, task_id).status == "blocked"
        # No token was minted or spent to get here.
        row = conn.execute("SELECT gate_token_hash FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert row["gate_token_hash"] is None


def test_reject_records_the_reason_on_the_board(kb_real, bridge):
    """The board is the only record of why a worker's request was discarded."""
    kb = kb_real
    with kb.connect() as conn:
        task_id, action_id = _card_with_pending_exact_action(kb, conn)

    bridge._reject_exact_action_payload(
        task_id, {"pending_action_id": action_id, "reason": "Analyst hält sie für unnötig"}
    )

    with kb.connect() as conn:
        bodies = [r[0] for r in conn.execute(
            "SELECT body FROM task_comments WHERE task_id=? ORDER BY id", (task_id,)
        ).fetchall()]
    assert any("Analyst hält sie für unnötig" in b for b in bodies)
    assert any("ABGELEHNT" in b for b in bodies)


def test_reject_without_a_reason_succeeds_and_is_marked(kb_real, bridge):
    """Operator decision 2026-07-16: the reason is optional. An empty one must
    still leave a deliberate mark on the board — not a silent gap."""
    kb = kb_real
    with kb.connect() as conn:
        task_id, action_id = _card_with_pending_exact_action(kb, conn)

    result = bridge._reject_exact_action_payload(
        task_id, {"pending_action_id": action_id, "reason": "   "}
    )

    assert result["ok"] is True
    with kb.connect() as conn:
        assert kb.get_pending_action(conn, task_id) is None
        assert kb.get_task(conn, task_id).status == "blocked"
        bodies = [r[0] for r in conn.execute(
            "SELECT body FROM task_comments WHERE task_id=? ORDER BY id", (task_id,)
        ).fetchall()]
    assert any("Ohne Begründung abgelehnt" in b for b in bodies)
    assert not any("Grund:  " in b for b in bodies)


def test_rejecting_an_already_rejected_action_is_gone_not_a_crash(kb_real, bridge):
    """Double-click / replay must be a clean 410, not a 500."""
    kb = kb_real
    with kb.connect() as conn:
        task_id, action_id = _card_with_pending_exact_action(kb, conn)

    body = {"pending_action_id": action_id, "reason": "weg damit"}
    assert bridge._reject_exact_action_payload(task_id, body)["ok"] is True

    with pytest.raises(bridge.KanbanGoneError):
        bridge._reject_exact_action_payload(task_id, body)


def test_reject_a_stale_action_id_is_refused(kb_real, bridge):
    """A card whose action was replaced must not honour the old id."""
    kb = kb_real
    with kb.connect() as conn:
        task_id, action_id = _card_with_pending_exact_action(kb, conn)

    with pytest.raises(RuntimeError):
        bridge._reject_exact_action_payload(
            task_id, {"pending_action_id": action_id + 999, "reason": "stale"}
        )

    with kb.connect() as conn:
        assert kb.get_pending_action(conn, task_id) is not None


def test_after_rejecting_the_card_can_finally_be_unblocked(kb_real, bridge):
    """End of the deadlock: reject settles the sticky action, unblock then works.

    Before WS1 this sequence was impossible from the UI -- approve refused on the
    gate, unblock refused because an action was pending.
    """
    kb = kb_real
    with kb.connect() as conn:
        task_id, action_id = _card_with_pending_exact_action(kb, conn, gated=True)

    bridge._reject_exact_action_payload(
        task_id, {"pending_action_id": action_id, "reason": "unnötig"}
    )

    with kb.connect() as conn:
        # The bridge's own unblock guard keys on detail["sticky"], not on the
        # detail being absent -- mirror that contract exactly.
        detail = bridge._sticky_block_detail(conn, task_id)
        assert not (detail and detail.get("sticky")), "sticky hold must be gone"
        task = kb.get_task(conn, task_id)
        assert bridge._unblock_gate_aware(conn, task_id, task, "geprüft, kann weiter")
        assert kb.get_task(conn, task_id).status in ("ready", "todo")


def test_blocker_details_advertises_the_reject_button(kb_real, bridge):
    """A route without an advertised action is invisible to the cockpit."""
    kb = kb_real
    with kb.connect() as conn:
        task_id, action_id = _card_with_pending_exact_action(kb, conn)
        details = bridge._blocker_details(conn, [kb.get_task(conn, task_id)])

    actions = details[task_id]["actions"]
    assert actions["reject_exact_action"]["available"] is True
    assert actions["reject_exact_action"]["endpoint"] == "reject-exact-action"


def test_reject_stays_available_on_an_approved_but_unconsumed_action(kb_real, bridge):
    """An approved grant is exactly what an operator may want to take back.

    Until it is settled the card cannot be unblocked either, so reject must not
    disappear the moment approval happened.
    """
    kb = kb_real
    with kb.connect() as conn:
        task_id, action_id = _card_with_pending_exact_action(kb, conn)
        att = kb.get_current_attention(conn, task_id)
        assert kb.approve_pending_action_and_unblock_versioned(
            conn, task_id, att.action_id, expected_version=att.version, actor="op",
        ).status == "approved"
        details = bridge._blocker_details(conn, [kb.get_task(conn, task_id)])
        assert details[task_id]["actions"]["approve_exact_action"]["available"] is False
        assert details[task_id]["actions"]["reject_exact_action"]["available"] is True

    result = bridge._reject_exact_action_payload(
        task_id, {"pending_action_id": action_id, "reason": "Freigabe zurückgezogen"}
    )
    assert result["ok"] is True
    with kb.connect() as conn:
        assert kb.get_pending_action_by_id(conn, task_id, action_id).state == "resolved"
