"""WS3 (2026-07-15): a technically parked approved action must be visible and solvable.

When a worker exceeds max_runtime_seconds while executing an ALREADY APPROVED
exact action, enforce_max_runtime kills it and the Core parks the card: the
action stays `approved`, the projection is swapped to capability/transient. This
runs on every dispatcher tick -- it is a live path, not a theory.

Two things were wrong:

1. The UI lied. get_pending_action does not filter by attention type, so the
   parked action looked exactly like one a worker was about to run, and the
   board said "Approved. Waiting for the resumed worker to execute the exact
   action." forever, about a worker that will never exist. The only clickable
   way out was archiving -- which CANCELS the approval the human already gave.

2. The escape existed but was uncallable. resume_approved_action_retry requires
   expected_origin_run_id, and no API ever emitted origin_run_id: the Attention
   dataclass had no such field and the dashboard's closed schema did not carry
   it. Its own test only passed by reading the value straight out of the DB.

NOTE: HERMES_HOME must stay off ~/.hermes -- a board rooted there writes into
the live kanban DB (get_default_hermes_root mapping).
"""

from __future__ import annotations

import importlib
import re
import sys
import time
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"


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


def _parked_approved_action(kb, conn):
    """Reproduce the real park: approve, claim, then fail the worker technically."""
    task_id = kb.create_task(conn, title="park", assignee="w")
    kb.claim_task(conn, task_id)
    run_id = kb.get_task(conn, task_id).current_run_id
    action = kb.record_pending_action(
        conn, task_id=task_id, run_id=run_id, command="git push origin main",
        summary="push", profile="w", workspace="/tmp", expires_at=int(time.time()) + 3600,
    )
    kb.block_task(conn, task_id, kind="needs_input", reason="approval", expected_run_id=run_id)
    att = kb.get_current_attention(conn, task_id)
    assert kb.approve_pending_action_and_unblock_versioned(
        conn, task_id, att.action_id, expected_version=att.version, actor="op",
    ).status == "approved"
    resumed = kb.claim_task(conn, task_id, claimer="retry-worker")
    origin_run_id = resumed.current_run_id
    # Exactly what enforce_max_runtime does (attention_type/reason_code match
    # kanban_db.py's own call site).
    assert kb.block_approved_action_for_technical_failure(
        conn, task_id=task_id, action_id=action.id, expected_run_id=origin_run_id,
        attention_type="transient", reason_code="runtime_timeout", now=int(time.time()),
    )
    return task_id, action.id, origin_run_id


def test_core_now_exposes_origin_run_id(kb_real):
    """Without this the only escape from the park has no callable signature."""
    kb = kb_real
    with kb.connect() as conn:
        task_id, _action_id, origin_run_id = _parked_approved_action(kb, conn)

        att = kb.get_current_attention(conn, task_id)

        assert att.type == "transient"
        assert att.origin_run_id == origin_run_id


def test_exact_action_attention_carries_no_origin_run_id(kb_real):
    """Behavioural neutrality: only a technical projection has an origin run."""
    kb = kb_real
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="x", assignee="w")
        kb.claim_task(conn, task_id)
        run_id = kb.get_task(conn, task_id).current_run_id
        kb.record_pending_action_and_block(
            conn, task_id=task_id, run_id=run_id, command="git push origin main",
            summary="push", profile="w", workspace="/tmp", expires_at=int(time.time()) + 3600,
        )
        att = kb.get_current_attention(conn, task_id)
        assert att.type == "exact_action"
        assert att.origin_run_id is None


def test_bridge_stops_claiming_a_worker_is_coming(kb_real, bridge):
    """The lie. This text was shown indefinitely for a worker that never exists."""
    kb = kb_real
    with kb.connect() as conn:
        task_id, _a, _o = _parked_approved_action(kb, conn)
        detail = bridge._blocker_details(conn, [kb.get_task(conn, task_id)])[task_id]

    assert detail["technical_park"] is True
    assert detail["attention_type"] == "transient"
    assert "Waiting for the resumed worker" not in detail["human_action"]
    assert "parked" in detail["human_action"]


def test_bridge_offers_the_retry_with_the_cas_handles(kb_real, bridge):
    """An operator cannot invent an attention version or a run id."""
    kb = kb_real
    with kb.connect() as conn:
        task_id, _a, origin_run_id = _parked_approved_action(kb, conn)
        att = kb.get_current_attention(conn, task_id)
        detail = bridge._blocker_details(conn, [kb.get_task(conn, task_id)])[task_id]

    resume = detail["actions"]["resume_approved_action_retry"]
    assert resume["available"] is True
    assert resume["endpoint"] == "resume-approved-action-retry"
    assert resume["attention_id"] == att.id
    assert resume["attention_version"] == att.version
    assert resume["origin_run_id"] == origin_run_id
    # Approving again is meaningless -- it is already approved.
    assert detail["actions"]["approve_exact_action"]["available"] is False


def test_retry_resumes_the_card_without_losing_the_approval(kb_real, bridge):
    """The point: archiving cancels the human's approval, retrying honours it."""
    kb = kb_real
    with kb.connect() as conn:
        task_id, action_id, origin_run_id = _parked_approved_action(kb, conn)
        att = kb.get_current_attention(conn, task_id)

    result = bridge._resume_approved_action_retry_payload(task_id, {
        "attention_id": att.id, "attention_version": att.version,
        "origin_run_id": origin_run_id,
    })

    assert result["ok"] is True
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status in ("ready", "todo")
        action = kb.get_pending_action_by_id(conn, task_id, action_id)
        assert action.state == "approved", "the approval must survive the retry"
        assert action.approved_at is not None
        assert kb.get_current_attention(conn, task_id).type == "exact_action"


def test_retry_is_versioned_and_replay_safe(kb_real, bridge):
    kb = kb_real
    with kb.connect() as conn:
        task_id, _a, origin_run_id = _parked_approved_action(kb, conn)
        att = kb.get_current_attention(conn, task_id)

    body = {"attention_id": att.id, "attention_version": att.version,
            "origin_run_id": origin_run_id}
    assert bridge._resume_approved_action_retry_payload(task_id, body)["ok"] is True

    # A replay must not re-arm anything.
    with pytest.raises((RuntimeError, LookupError, bridge.KanbanGoneError)):
        bridge._resume_approved_action_retry_payload(task_id, body)


def test_retry_rejects_a_wrong_origin_run(kb_real, bridge):
    kb = kb_real
    with kb.connect() as conn:
        task_id, _a, origin_run_id = _parked_approved_action(kb, conn)
        att = kb.get_current_attention(conn, task_id)

    with pytest.raises(RuntimeError):
        bridge._resume_approved_action_retry_payload(task_id, {
            "attention_id": att.id, "attention_version": att.version,
            "origin_run_id": origin_run_id + 99,
        })


def test_retry_requires_all_three_handles(kb_real, bridge):
    kb = kb_real
    with kb.connect() as conn:
        task_id, _a, _o = _parked_approved_action(kb, conn)

    for body in ({}, {"attention_id": 1}, {"attention_id": 1, "attention_version": 1}):
        with pytest.raises(ValueError):
            bridge._resume_approved_action_retry_payload(task_id, body)


def test_an_ordinary_pending_action_is_not_a_park(kb_real, bridge):
    """Behavioural neutrality: a normal approval flow must be untouched."""
    kb = kb_real
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="normal", assignee="w")
        kb.claim_task(conn, task_id)
        run_id = kb.get_task(conn, task_id).current_run_id
        kb.record_pending_action_and_block(
            conn, task_id=task_id, run_id=run_id, command="git push origin main",
            summary="push", profile="w", workspace="/tmp", expires_at=int(time.time()) + 3600,
        )
        detail = bridge._blocker_details(conn, [kb.get_task(conn, task_id)])[task_id]

    assert detail["technical_park"] is False
    assert detail["actions"]["resume_approved_action_retry"]["available"] is False
    assert detail["actions"]["approve_exact_action"]["available"] is True


# --- board view -----------------------------------------------------------

def test_board_renders_the_retry_button_and_passes_the_handles():
    panels = (STATIC / "panels.js").read_text(encoding="utf-8")
    assert "resumeApprovedActionRetry(event," in panels
    handler = panels[panels.index("async function resumeApprovedActionRetry"):]
    handler = handler[:handler.index("// Reopening lands the card")]
    assert "attention_id: attentionId" in handler
    assert "attention_version: attentionVersion" in handler
    assert "origin_run_id: originRunId" in handler
    assert "_kanbanBoardQuery()" in handler
    row = re.search(r'<div class="kanban-status-actions">(.*?)</div>', panels, re.S)
    assert "resumeButton" in row.group(1)


@pytest.mark.parametrize("key", [
    "kanban_resume_retry", "kanban_resume_retry_hint",
    "kanban_resume_retry_done", "kanban_resume_retry_unavailable",
])
def test_every_language_block_defines_the_retry_keys(key):
    i18n = (STATIC / "i18n.js").read_text(encoding="utf-8")
    expected = len(re.findall(r"^\s*kanban_approve_exact_action:", i18n, re.M))
    assert len(re.findall(rf"^\s*{key}:", i18n, re.M)) == expected
