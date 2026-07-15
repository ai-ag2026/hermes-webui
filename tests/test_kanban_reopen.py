"""WS2 (2026-07-15): reopening a done/archived card from the WebUI.

Card t_bccbacc4 was auto-completed by a rerun against its own analyst's explicit
advice. Getting it back required a raw status write, because no verb accepts a
terminal source status. The raw path (_set_status_direct, which has no source
restriction at all) leaves completed_at/result standing, emits no `reopened`
event, and -- in this bridge's copy -- does not demote children that were ready
only because the parent was done.

NOTE: HERMES_HOME must stay off ~/.hermes -- a board rooted there writes into
the live kanban DB (get_default_hermes_root mapping).
"""

from __future__ import annotations

import importlib
import re
import sys
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


def _completed(kb, conn, title="audit"):
    task_id = kb.create_task(conn, title=title, assignee="analyst")
    kb.claim_task(conn, task_id)
    assert kb.complete_task(conn, task_id, result="fertig")
    return task_id


def test_reopen_route_returns_the_card_to_blocked(kb_real, bridge):
    kb = kb_real
    with kb.connect() as conn:
        task_id = _completed(kb, conn)

    result = bridge._reopen_task_payload(task_id, {"reason": "Auto-Completion zurückgenommen"})

    assert result["ok"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task.status == "blocked"
        assert task.completed_at is None
        assert task.result is None


def test_reopened_card_does_not_become_dispatchable(kb_real, bridge):
    """The 2026-07-15 failure mode, end to end through the bridge."""
    kb = kb_real
    with kb.connect() as conn:
        task_id = _completed(kb, conn)

    bridge._reopen_task_payload(task_id, {"reason": "warte auf Entscheidung"})

    with kb.connect() as conn:
        kb.recompute_ready(conn)
        assert kb.get_task(conn, task_id).status == "blocked"


def test_reopen_requires_a_reason(kb_real, bridge):
    kb = kb_real
    with kb.connect() as conn:
        task_id = _completed(kb, conn)

    for bad in ({}, {"reason": ""}, {"reason": "  "}):
        with pytest.raises(ValueError):
            bridge._reopen_task_payload(task_id, bad)

    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "done", "refusal must be inert"


def test_reopen_refuses_a_non_terminal_card(kb_real, bridge):
    kb = kb_real
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="laeuft", assignee="w")
        kb.claim_task(conn, task_id)

    with pytest.raises(RuntimeError, match="done or archived"):
        bridge._reopen_task_payload(task_id, {"reason": "x"})


def test_reopen_rejects_a_ready_target(kb_real, bridge):
    kb = kb_real
    with kb.connect() as conn:
        task_id = _completed(kb, conn)

    with pytest.raises(ValueError):
        bridge._reopen_task_payload(task_id, {"reason": "x", "to_status": "ready"})


def test_raw_status_patch_can_no_longer_reopen_a_done_card(kb_real, bridge):
    """The silent alternative must be closed, or the verb is just a suggestion.

    _set_status_direct has no source-status restriction, so before this a drag
    from Done to Todo quietly reopened the card with completed_at still set and
    no audit trail.
    """
    kb = kb_real
    with kb.connect() as conn:
        task_id = _completed(kb, conn)

        for target in ("todo", "ready", "triage"):
            with pytest.raises(RuntimeError, match="reopen"):
                bridge._patch_task(conn, task_id, {"status": target})

        task = kb.get_task(conn, task_id)
        assert task.status == "done"
        assert task.completed_at is not None, "the refusal must not half-apply"


def test_ordinary_status_moves_still_work(kb_real, bridge):
    """Behavioural neutrality: the guard keys on a terminal SOURCE, nothing else."""
    kb = kb_real
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="normal", assignee="w")
        bridge._patch_task(conn, task_id, {"status": "todo"})
        assert kb.get_task(conn, task_id).status == "todo"
        bridge._patch_task(conn, task_id, {"status": "ready"})
        assert kb.get_task(conn, task_id).status == "ready"
        bridge._patch_task(conn, task_id, {"status": "triage"})
        assert kb.get_task(conn, task_id).status == "triage"


def test_archiving_a_done_card_still_works(kb_real, bridge):
    """Terminal -> terminal is not a reopen and must stay available."""
    kb = kb_real
    with kb.connect() as conn:
        task_id = _completed(kb, conn)
        bridge._patch_task(conn, task_id, {"status": "archived"})
        assert kb.get_task(conn, task_id).status == "archived"


# --- board view -----------------------------------------------------------

def test_board_renders_a_reopen_button_only_on_terminal_cards():
    panels = (STATIC / "panels.js").read_text(encoding="utf-8")
    assert "reopenKanbanTask(event," in panels
    assert re.search(
        r"reopenButton\s*=\s*\(taskStatus === 'done' \|\| taskStatus === 'archived'\)",
        panels,
    ), "reopen must only be offered where it applies"
    row = re.search(r'<div class="kanban-status-actions">(.*?)</div>', panels, re.S)
    assert "reopenButton" in row.group(1)


def test_board_reopen_asks_for_a_reason_and_targets_blocked():
    panels = (STATIC / "panels.js").read_text(encoding="utf-8")
    handler = panels[panels.index("async function reopenKanbanTask"):]
    handler = handler[:handler.index("async function loadKanbanTask")]
    assert "showPromptDialog(" in handler
    assert "if (reason == null) return;" in handler
    assert "kanban_reopen_reason_required" in handler
    assert "to_status: 'blocked'" in handler, (
        "the board must never reopen straight to ready -- the dispatcher would claim it"
    )
    assert "_kanbanBoardQuery()" in handler


@pytest.mark.parametrize("key", [
    "kanban_reopen", "kanban_reopen_hint", "kanban_reopen_prompt",
    "kanban_reopen_placeholder", "kanban_reopen_reason_required", "kanban_reopen_done",
])
def test_every_language_block_defines_the_reopen_keys(key):
    i18n = (STATIC / "i18n.js").read_text(encoding="utf-8")
    expected = len(re.findall(r"^\s*kanban_approve_exact_action:", i18n, re.M))
    assert len(re.findall(rf"^\s*{key}:", i18n, re.M)) == expected
