"""WS4 (2026-07-15): the last two operator dead ends.

Both were audit claims, verified empirically before being fixed:

(a) Complete on a human-gated card. _patch_task passed no token, so the Core
    refused and the operator got the raw gate error -- which advises waiting for
    an ntfy push or running `hermes kanban gate <id> off` in a shell: a CLI
    command with no WebUI equivalent. Not a bypass (the card stayed blocked),
    just a one-click path that could never work and advice nobody could follow.

(b) Unarchive had no route on this surface at all, only `hermes kanban
    unarchive`. Archiving is one click behind a 4-second confirm and, since the
    H2 removal, needs no grant even for a running card. For an operator without
    CLI access, archived was a one-way street.

Unarchive is deliberately NOT reopen: reopen says "not finished after all" and
clears completed_at/result; unarchive says "archived by mistake" and must keep
them. The result is evidence.

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


# --- (a) completing a gated card ------------------------------------------

def test_completing_a_gated_card_works(kb_real, bridge):
    """The regression: this raised the raw GateTokenError before the fix."""
    kb = kb_real
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="gated", assignee="w")
        kb.claim_task(conn, task_id)
        kb.block_task(conn, task_id, kind="needs_input", reason="gate", human_gate=True)

        bridge._patch_task(conn, task_id, {"status": "done", "result": "vom Operator abgeschlossen"})

        task = kb.get_task(conn, task_id)
        assert task.status == "done"
        assert bool(task.human_gate) is True, "the gate flag itself persists"
        row = conn.execute("SELECT gate_token_hash FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert row["gate_token_hash"] is None, "the one-shot grant must be consumed, not left open"


def test_completing_a_gated_card_is_recorded(kb_real, bridge):
    """A gate release with no trace is how 2026-07-13 became unreconstructable."""
    kb = kb_real
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="gated", assignee="w")
        kb.claim_task(conn, task_id)
        kb.block_task(conn, task_id, kind="needs_input", reason="gate", human_gate=True)

        bridge._patch_task(conn, task_id, {"status": "done"})

        bodies = [r[0] for r in conn.execute(
            "SELECT body FROM task_comments WHERE task_id=? ORDER BY id", (task_id,)
        ).fetchall()]
    assert any("GATE-FREIGABE" in b and "Abschluss" in b for b in bodies)


def test_completing_an_ungated_card_mints_nothing(kb_real, bridge):
    """Behavioural neutrality: the ungated path is byte-identical."""
    kb = kb_real
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="normal", assignee="w")
        kb.claim_task(conn, task_id)

        bridge._patch_task(conn, task_id, {"status": "done"})

        assert kb.get_task(conn, task_id).status == "done"
        row = conn.execute("SELECT gate_token_hash FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert row["gate_token_hash"] is None
        bodies = [r[0] for r in conn.execute(
            "SELECT body FROM task_comments WHERE task_id=?", (task_id,)
        ).fetchall()]
        assert not any("GATE-FREIGABE" in b for b in bodies), "no gate, no gate comment"


def test_completing_a_gated_card_still_honours_the_exact_action_hold(kb_real, bridge):
    """Minting for the gate must not become a way past the exact-action gate.

    A live exact action is a different gate class and complete_task refuses on
    it regardless of any token -- test_kanban_a1_exact_action_gate.py exists to
    protect exactly this.
    """
    kb = kb_real
    import time
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="danger", assignee="w")
        kb.claim_task(conn, task_id)
        run_id = kb.get_task(conn, task_id).current_run_id
        kb.record_pending_action_and_block(
            conn, task_id=task_id, run_id=run_id, command="git push origin main",
            summary="push", profile="w", workspace="/tmp", expires_at=int(time.time()) + 3600,
        )
        kb.set_human_gate(conn, task_id, on=True, actor="test")

        with pytest.raises(RuntimeError):
            bridge._patch_task(conn, task_id, {"status": "done"})

        assert kb.get_task(conn, task_id).status == "blocked"
        assert kb.get_pending_action(conn, task_id).state == "pending"


# --- (b) unarchive --------------------------------------------------------

def _archived_done_card(kb, conn):
    task_id = kb.create_task(conn, title="versehentlich archiviert", assignee="w")
    kb.claim_task(conn, task_id)
    kb.complete_task(conn, task_id, result="Report liegt in REPORT.md")
    kb.archive_task(conn, task_id)
    assert kb.get_task(conn, task_id).status == "archived"
    return task_id


def test_unarchive_restores_the_card_as_it_was(kb_real, bridge):
    kb = kb_real
    with kb.connect() as conn:
        task_id = _archived_done_card(kb, conn)

    result = bridge._unarchive_task_payload(task_id, {})

    assert result["ok"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task.status == "done", "a card archived from done belongs back in done"
        assert task.result == "Report liegt in REPORT.md", "the result is evidence -- it must survive"


def test_unarchive_is_not_reopen(kb_real, bridge):
    """The distinction that makes both verbs necessary."""
    kb = kb_real
    with kb.connect() as conn:
        archived = _archived_done_card(kb, conn)
        reopened = _archived_done_card(kb, conn)

    bridge._unarchive_task_payload(archived, {})
    bridge._reopen_task_payload(reopened, {"reason": "doch nicht fertig"})

    with kb.connect() as conn:
        kept = kb.get_task(conn, archived)
        cleared = kb.get_task(conn, reopened)
        assert kept.status == "done" and kept.result is not None
        assert cleared.status == "blocked" and cleared.result is None


def test_unarchive_refuses_a_card_that_is_not_archived(kb_real, bridge):
    kb = kb_real
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="offen", assignee="w")

    with pytest.raises(RuntimeError, match="archived"):
        bridge._unarchive_task_payload(task_id, {})


def test_unarchive_rejects_an_invalid_target(kb_real, bridge):
    kb = kb_real
    with kb.connect() as conn:
        task_id = _archived_done_card(kb, conn)

    with pytest.raises(ValueError):
        bridge._unarchive_task_payload(task_id, {"to_status": "running"})


# --- board view -----------------------------------------------------------

def test_board_offers_one_obvious_action_per_terminal_column():
    """reopen on done, unarchive on archived -- not both everywhere."""
    panels = (STATIC / "panels.js").read_text(encoding="utf-8")
    assert "unarchiveKanbanTask(event," in panels
    assert re.search(r"reopenButton\s*=\s*taskStatus === 'done'", panels)
    assert re.search(r"unarchiveButton\s*=\s*taskStatus === 'archived'", panels)
    row = re.search(r'<div class="kanban-status-actions">(.*?)</div>', panels, re.S)
    assert "unarchiveButton" in row.group(1)


def test_unarchive_handler_posts_to_its_own_route():
    panels = (STATIC / "panels.js").read_text(encoding="utf-8")
    handler = panels[panels.index("async function unarchiveKanbanTask"):]
    handler = handler[:handler.index("// The escape from a technical park")]
    assert "'/unarchive'" in handler
    assert "_kanbanBoardQuery()" in handler


@pytest.mark.parametrize("key", ["kanban_unarchive", "kanban_unarchive_hint", "kanban_unarchive_done"])
def test_every_language_block_defines_the_unarchive_keys(key):
    i18n = (STATIC / "i18n.js").read_text(encoding="utf-8")
    expected = len(re.findall(r"^\s*kanban_approve_exact_action:", i18n, re.M))
    assert len(re.findall(rf"^\s*{key}:", i18n, re.M)) == expected
