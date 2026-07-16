"""Unblock a card that carries a typed non-exact attention (2026-07-16).

The Core's legacy ``unblock_task`` fail-closes on ANY ``task_attentions`` row
("legacy unblock owns only ordinary blocked cards"). Since the circuit breaker
started leaving typed attentions (decision/gave_up/protocol) on the cards it
parks, the cockpit's Entsperren button was dead for exactly those cards:
/unblock -> "unblock refused" -> the cockpit's friendly toast claimed "die
Karte hat den Status gewechselt. Aktualisiere kurz." — but no refresh could
ever fix it (live repro: t_05ad5273, blocked + protocol attention).

The bridge must route those cards through
``transition_task_status_with_attention`` — the Core's only safe generic
blocked exit — exactly like the agent dashboard does (plugin_api.update_task).

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


def _blocked_with_typed_attention(kb, conn, *, attention_type="protocol"):
    """Reproduce the circuit-breaker park: blocked card + non-exact projection."""
    task_id = kb.create_task(conn, title="parked by breaker", assignee="w")
    kb.block_task(conn, task_id, kind="needs_input", reason="failure limit reached")
    att = kb.upsert_current_typed_attention(
        conn, task_id=task_id, attention_type=attention_type,
        reason_code="goal_closeout_missing", summary="goal_closeout_missing",
    )
    assert att.action_id is None
    return task_id, att


def test_core_refuses_legacy_unblock_for_typed_attention(kb_real):
    """The premise: without the seam the card is stuck (this is the live bug)."""
    kb = kb_real
    with kb.connect() as conn:
        task_id, _att = _blocked_with_typed_attention(kb, conn)
        assert kb.unblock_task(conn, task_id) is False
        assert kb.get_task(conn, task_id).status == "blocked"


def test_unblock_releases_typed_attention_card(kb_real, bridge):
    """The fix: the cockpit's /unblock releases a breaker-parked card."""
    kb = kb_real
    with kb.connect() as conn:
        task_id, _att = _blocked_with_typed_attention(kb, conn)

    result = bridge._task_action_payload(task_id, {"reason": "geprüft, weiter"}, "unblock")

    assert result["task"]["status"] == "ready"
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "ready"
        assert kb.get_current_attention(conn, task_id) is None
        comments = [c.body for c in kb.list_comments(conn, task_id)]
        assert any("UNBLOCK via WebUI-Cockpit" in c for c in comments)


def test_unblock_honours_the_cockpit_snapshot_cas(kb_real, bridge):
    """A stale snapshot must conflict, not silently resolve a newer attention."""
    kb = kb_real
    with kb.connect() as conn:
        task_id, att = _blocked_with_typed_attention(kb, conn)

    body = {"attention_id": att.id, "attention_version": att.version + 7}
    with pytest.raises(RuntimeError, match="refresh"):
        bridge._task_action_payload(task_id, body, "unblock")
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "blocked"
        assert kb.get_current_attention(conn, task_id) is not None

    # The true pair releases it.
    body = {"attention_id": att.id, "attention_version": att.version}
    result = bridge._task_action_payload(task_id, body, "unblock")
    assert result["task"]["status"] == "ready"


def test_unblock_gated_typed_attention_mints_change_status_token(kb_real, bridge):
    """transition_* enforces the gate under action="change_status" — a token
    minted for "unblock" is a hard rejection, so the bridge must mint the
    matching action and the release must still be a one-click success."""
    kb = kb_real
    with kb.connect() as conn:
        task_id, _att = _blocked_with_typed_attention(kb, conn)
        kb.set_human_gate(conn, task_id, on=True)

    result = bridge._task_action_payload(task_id, {}, "unblock")

    assert result["task"]["status"] == "ready"
    with kb.connect() as conn:
        comments = [c.body for c in kb.list_comments(conn, task_id)]
        assert any("GATE-FREIGABE via WebUI-Cockpit" in c for c in comments)


def test_unblock_without_attention_keeps_legacy_path(kb_real, bridge):
    """Behavioural neutrality: ordinary blocked cards release exactly as before."""
    kb = kb_real
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="plain block", assignee="w")
        kb.block_task(conn, task_id, reason="waiting")
        assert kb.get_current_attention(conn, task_id) is None

    result = bridge._task_action_payload(task_id, {}, "unblock")
    assert result["task"]["status"] in ("ready", "todo")


def test_unblock_rejects_non_integer_cas_handles(kb_real, bridge):
    kb = kb_real
    with kb.connect() as conn:
        task_id, _att = _blocked_with_typed_attention(kb, conn)

    with pytest.raises(ValueError):
        bridge._task_action_payload(task_id, {"attention_id": "abc", "attention_version": 1}, "unblock")


def test_plain_unblock_payload_carries_the_cas_pair(kb_real, bridge):
    """The cockpit can only echo the pair if the payload exposes it."""
    kb = kb_real
    with kb.connect() as conn:
        task_id, att = _blocked_with_typed_attention(kb, conn)
        detail = bridge._blocker_details(conn, [kb.get_task(conn, task_id)])[task_id]

    plain = detail["actions"]["plain_unblock"]
    assert plain["allowed"] is True
    assert plain["attention_id"] == att.id
    assert plain["attention_version"] == att.version


def test_exact_action_cards_stay_on_the_approval_path(kb_real, bridge):
    """Sticky cards must keep refusing plain unblock — approval is the only exit."""
    kb = kb_real
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="exact", assignee="w")
        kb.claim_task(conn, task_id)
        run_id = kb.get_task(conn, task_id).current_run_id
        kb.record_pending_action_and_block(
            conn, task_id=task_id, run_id=run_id, command="git push origin main",
            summary="push", profile="w", workspace="/tmp",
            expires_at=int(time.time()) + 3600,
        )

    with pytest.raises(RuntimeError, match="exact terminal action"):
        bridge._task_action_payload(task_id, {}, "unblock")
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "blocked"
