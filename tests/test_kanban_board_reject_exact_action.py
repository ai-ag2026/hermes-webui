"""WS1 (2026-07-15): the Kanban board view must offer "reject", not only "approve".

The cockpit extension is a floating blocker drawer; the board in static/panels.js
is where an operator actually works. Both had an approve control and neither had
a reject control, so on card t_bccbacc4 -- where the agent that raised the exact
action declared it unnecessary itself and recommended rejecting it -- there was
no way to say no. The action had to be resolved with a direct DB script.

These tests pin the board-side contract as static assertions over panels.js /
i18n.js, matching how the rest of this suite covers the static frontend.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"


@pytest.fixture(scope="module")
def panels() -> str:
    return (STATIC / "panels.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def i18n() -> str:
    return (STATIC / "i18n.js").read_text(encoding="utf-8")


def test_detail_view_renders_a_reject_button_next_to_approve(panels):
    assert "rejectExactKanbanAction(event," in panels
    # It must sit in the same action row as the approve button, not somewhere
    # the operator has to hunt for it.
    row = re.search(r'<div class="kanban-status-actions">(.*?)</div>', panels, re.S)
    assert row, "kanban-status-actions row not found"
    assert "rejectButton" in row.group(1)
    assert "approveButton" in row.group(1)


def test_reject_button_is_gated_on_the_bridge_advertising_the_action(panels):
    """A button that ignores actions.reject_exact_action.available would lie."""
    assert "detail.actions && detail.actions.reject_exact_action" in panels
    assert re.search(
        r"rejectButton\s*=\s*detail && detail\.sticky && exactReject && exactReject\.available",
        panels,
    ), "reject button must only render when the core+bridge can actually reject"


def test_reject_posts_to_its_own_endpoint_with_a_reason(panels):
    assert "'/reject-exact-action'" in panels
    handler = panels[panels.index("async function rejectExactKanbanAction"):]
    handler = handler[:handler.index("async function loadKanbanTask")]
    assert "pending_action_id: pendingActionId" in handler
    assert "reason: String(reason).trim()" in handler
    assert "_kanbanBoardQuery()" in handler, "multi-board routing must be preserved"


def test_reject_requires_a_reason_and_can_be_cancelled(panels):
    handler = panels[panels.index("async function rejectExactKanbanAction"):]
    handler = handler[:handler.index("async function loadKanbanTask")]
    assert "showPromptDialog(" in handler, "reason is collected, not assumed"
    assert "danger: true" in handler
    assert "if (reason == null) return;" in handler, "cancel must abort, not send empty"
    assert "kanban_reject_exact_action_reason_required" in handler


def test_reject_shares_the_approval_inflight_guard(panels):
    """Approving and rejecting the same action must not fly concurrently."""
    handler = panels[panels.index("async function rejectExactKanbanAction"):]
    handler = handler[:handler.index("async function loadKanbanTask")]
    assert "_kanbanExactApprovalInflight.has(key)" in handler
    assert "_kanbanExactApprovalInflight.add(key)" in handler
    assert "_kanbanExactApprovalInflight.delete(key)" in handler


@pytest.mark.parametrize("key", [
    "kanban_reject_exact_action",
    "kanban_reject_exact_action_hint",
    "kanban_reject_exact_action_prompt",
    "kanban_reject_exact_action_placeholder",
    "kanban_reject_exact_action_reason_required",
    "kanban_reject_exact_action_done",
])
def test_every_language_block_defines_the_new_keys(i18n, key):
    """A missing key renders as a blank button in that locale.

    Anchor on an existing exact-action key: however many language blocks that
    one has, the new ones must have exactly as many.
    """
    expected = len(re.findall(r"^\s*kanban_approve_exact_action:", i18n, re.M))
    assert expected >= 10, "sanity: the anchor key should exist in every locale"
    assert len(re.findall(rf"^\s*{key}:", i18n, re.M)) == expected


def test_german_strings_are_actually_german(i18n):
    """de is the operator's language here -- an English placeholder is a bug."""
    de_block = i18n[i18n.index("\n  de: {"):]
    de_block = de_block[:de_block.index("\n  zh: {")]
    assert "kanban_reject_exact_action: 'Exakten Terminalbefehl ablehnen'" in de_block
    # The prompt must state the two things that surprise people most:
    # nothing runs, and the card does NOT get released.
    prompt = re.search(r"kanban_reject_exact_action_prompt: '([^']*)'", de_block).group(1)
    assert "nicht ausgef" in prompt
    assert "blockiert" in prompt
