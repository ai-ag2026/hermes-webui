"""Kanban notify poller tests.

The poller is the WebUI-side consumer for ``kanban_notify_subs`` rows with
``platform='webui'`` — the rows the gateway notifier can never serve. These
tests drive ``run_tick()`` against a fake ``kanban_db`` (CI does not install
hermes-agent, mirroring test_kanban_bridge.py) and a patched
``_start_turn`` so no real agent turn is spawned.

Contract under test:
  - completed events wake the creating session exactly once, with the task id
    and handoff in the prompt, and drop the subscription of a done task;
  - several subscriptions of one session are batched into ONE turn;
  - silent kinds (status/archived/unblocked) are claimed but never wake;
  - 409 (busy) rewinds the cursor and retries after the backoff window;
  - 404 (session deleted) drops the subscription instead of retrying forever;
  - repeated turn-start failures cap out and drop the subscription.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field

import pytest

from api import kanban_notify_poller as poller


@dataclass
class FakeTask:
    id: str
    title: str
    status: str = "done"
    result: str | None = None


@dataclass
class FakeEvent:
    id: int
    task_id: str
    kind: str
    payload: dict | None = None
    created_at: int = 1_000_000
    run_id: int | None = None


@dataclass
class FakeSub:
    task_id: str
    chat_id: str
    platform: str = "webui"
    thread_id: str = ""
    last_event_id: int = 0

    def as_row(self) -> dict:
        return {
            "task_id": self.task_id,
            "platform": self.platform,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "last_event_id": self.last_event_id,
            "notifier_profile": "",
        }


@dataclass
class FakeKB:
    """In-memory stand-in for hermes_cli.kanban_db, single board."""

    subs: list[FakeSub] = field(default_factory=list)
    events: list[FakeEvent] = field(default_factory=list)
    tasks: dict[str, FakeTask] = field(default_factory=dict)
    rewinds: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def list_boards(self, *, include_archived=True):
        return [{"slug": "default"}]

    def kanban_db_path(self, *, board=None):
        return "/fake/kanban.db"

    def list_notify_subs(self, conn, task_id=None, *, include_inactive=False):
        return [s.as_row() for s in self.subs]

    def _sub(self, task_id, chat_id):
        for s in self.subs:
            if s.task_id == task_id and s.chat_id == chat_id:
                return s
        return None

    def _unseen(self, sub, kinds):
        kinds = set(kinds or ())
        evs = [e for e in self.events
               if e.task_id == sub.task_id and e.id > sub.last_event_id
               and (not kinds or e.kind in kinds)]
        new_cursor = max((e.id for e in evs), default=sub.last_event_id)
        return new_cursor, evs

    def unseen_events_for_sub(self, conn, *, task_id, platform, chat_id,
                              thread_id=None, kinds=None):
        sub = self._sub(task_id, chat_id)
        if sub is None:
            return 0, []
        return self._unseen(sub, kinds)

    def claim_unseen_events_for_sub(self, conn, *, task_id, platform, chat_id,
                                    thread_id=None, kinds=None):
        sub = self._sub(task_id, chat_id)
        if sub is None:
            return 0, 0, []
        old = sub.last_event_id
        new_cursor, evs = self._unseen(sub, kinds)
        if not evs:
            return old, old, []
        sub.last_event_id = new_cursor
        return old, new_cursor, evs

    def rewind_notify_cursor(self, conn, *, task_id, platform, chat_id,
                             thread_id=None, claimed_cursor, old_cursor):
        sub = self._sub(task_id, chat_id)
        if sub is None or sub.last_event_id != claimed_cursor:
            return False
        sub.last_event_id = old_cursor
        self.rewinds.append(task_id)
        return True

    def remove_notify_sub(self, conn, *, task_id, platform, chat_id, thread_id=None):
        sub = self._sub(task_id, chat_id)
        if sub is None:
            return False
        self.subs.remove(sub)
        self.removed.append(task_id)
        return True

    def get_task(self, conn, task_id):
        return self.tasks.get(task_id)


@pytest.fixture
def fake_env(monkeypatch):
    """Wire a FakeKB into the poller and capture started turns."""
    kb = FakeKB()
    turns: list[tuple[str, str]] = []
    responses: list[dict] = []

    def fake_start_turn(session_id, message):
        turns.append((session_id, message))
        return responses.pop(0) if responses else {"_status": 200}

    monkeypatch.setattr(poller, "_kanban_db", lambda: kb)
    monkeypatch.setattr(poller, "_start_turn", fake_start_turn)
    import api.kanban_bridge as bridge
    monkeypatch.setattr(bridge, "_conn", lambda board=None: contextlib.nullcontext(None))
    poller._BACKOFF_UNTIL.clear()
    poller._FAILURES.clear()
    return kb, turns, responses


def test_completed_event_wakes_session_and_unsubscribes(fake_env):
    kb, turns, _ = fake_env
    kb.subs.append(FakeSub("t_aaa", "sess1"))
    kb.tasks["t_aaa"] = FakeTask("t_aaa", "Recherche X", status="done",
                                 result="Erste Zeile des Ergebnisses\nRest")
    kb.events.append(FakeEvent(7, "t_aaa", "completed",
                               payload={"summary": "Alles erledigt, Artefakt liegt vor"}))

    assert poller.run_tick() == 1
    assert len(turns) == 1
    session_id, message = turns[0]
    assert session_id == "sess1"
    assert "t_aaa" in message
    assert "Recherche X" in message
    assert "Alles erledigt" in message          # payload summary wins over task.result
    assert message.startswith("[IMPORTANT:")
    assert kb.removed == ["t_aaa"]              # done task → subscription dropped
    assert not kb.rewinds


def test_multiple_subs_one_session_one_turn(fake_env):
    kb, turns, _ = fake_env
    for tid in ("t_one", "t_two"):
        kb.subs.append(FakeSub(tid, "sess1"))
        kb.tasks[tid] = FakeTask(tid, f"Task {tid}")
        kb.events.append(FakeEvent(len(kb.events) + 1, tid, "completed"))

    assert poller.run_tick() == 1
    assert len(turns) == 1
    _, message = turns[0]
    assert "t_one" in message and "t_two" in message


def test_silent_kinds_consumed_without_waking(fake_env):
    kb, turns, _ = fake_env
    sub = FakeSub("t_sil", "sess1")
    kb.subs.append(sub)
    kb.tasks["t_sil"] = FakeTask("t_sil", "Leise", status="running")
    kb.events.append(FakeEvent(3, "t_sil", "status", payload={"status": "running"}))
    kb.events.append(FakeEvent(4, "t_sil", "unblocked"))

    assert poller.run_tick() == 0
    assert not turns
    assert sub.last_event_id == 4               # claimed, cursor advanced
    assert not kb.rewinds                       # not rewound: consumed silently
    assert not kb.removed                       # task not done → sub stays


def test_busy_session_rewinds_and_retries_after_backoff(fake_env, monkeypatch):
    kb, turns, responses = fake_env
    sub = FakeSub("t_bsy", "sess1")
    kb.subs.append(sub)
    kb.tasks["t_bsy"] = FakeTask("t_bsy", "Busy")
    kb.events.append(FakeEvent(5, "t_bsy", "completed"))
    responses.append({"_status": 409, "error": "session already has an active stream"})

    assert poller.run_tick() == 0
    assert len(turns) == 1
    assert kb.rewinds == ["t_bsy"]
    assert sub.last_event_id == 0               # cursor restored: durable retry queue

    # Within the backoff window the subscription is skipped entirely.
    assert poller.run_tick() == 0
    assert len(turns) == 1

    # After the backoff expires the same event is redelivered.
    poller._BACKOFF_UNTIL.clear()
    assert poller.run_tick() == 1
    assert len(turns) == 2
    assert sub.last_event_id == 5


def test_deleted_session_drops_subscription(fake_env):
    kb, turns, responses = fake_env
    kb.subs.append(FakeSub("t_del", "gone"))
    kb.tasks["t_del"] = FakeTask("t_del", "Weg")
    kb.events.append(FakeEvent(2, "t_del", "completed"))
    responses.append({"_status": 404, "error": "Session not found"})

    assert poller.run_tick() == 0
    assert kb.removed == ["t_del"]
    assert not kb.rewinds                       # no rewind: destination unreachable forever


def test_paused_wakeups_back_off_long(fake_env):
    kb, turns, responses = fake_env
    sub = FakeSub("t_pau", "sess1")
    kb.subs.append(sub)
    kb.tasks["t_pau"] = FakeTask("t_pau", "Pause")
    kb.events.append(FakeEvent(9, "t_pau", "completed"))
    responses.append({"_status": 409, "error": "process_wakeup_paused"})

    assert poller.run_tick() == 0
    assert kb.rewinds == ["t_pau"]
    assert sub.last_event_id == 0
    backoff = poller._BACKOFF_UNTIL.get("sess1")
    assert backoff is not None
    import time
    assert backoff - time.monotonic() > poller._RETRY_BACKOFF_SECONDS


def test_repeated_failures_cap_and_drop_subscription(fake_env):
    kb, turns, responses = fake_env
    kb.subs.append(FakeSub("t_bad", "sess1"))
    kb.tasks["t_bad"] = FakeTask("t_bad", "Kaputt")
    kb.events.append(FakeEvent(4, "t_bad", "completed"))

    for i in range(poller._MAX_CONSECUTIVE_FAILURES):
        responses.append({"_status": 500})
        poller._BACKOFF_UNTIL.clear()
        poller.run_tick()

    assert kb.removed == ["t_bad"]
    assert len(turns) == poller._MAX_CONSECUTIVE_FAILURES
