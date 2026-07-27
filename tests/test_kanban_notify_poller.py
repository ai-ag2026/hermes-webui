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
    appended_events: list = field(default_factory=list)

    def _append_event(self, conn, task_id, kind, payload):
        self.appended_events.append(
            FakeEvent(len(self.appended_events) + 1000, task_id, kind, payload)
        )

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
    # New contract (TARS review 2026-07-27): giving up on the CHANNEL must not
    # also consume the task's events — rewind first, then record the loss.
    assert kb.rewinds == ["t_del"]
    assert any(e.kind == "notify_delivery_failed" for e in kb.appended_events)


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


# ---------------------------------------------------------------------------
# Repair round after the TARS review (2026-07-27)
# ---------------------------------------------------------------------------

def test_shutdown_rewinds_unfinalized_claims(fake_env, monkeypatch):
    """A tick interrupted by shutdown must not swallow claimed events.

    The claim advances the durable cursor before the turn starts; without a
    drain, a WebUI restart mid-tick loses that batch silently.
    """
    kb, turns, _ = fake_env
    sub = FakeSub("t_shut", "sess1")
    kb.subs.append(sub)
    kb.tasks["t_shut"] = FakeTask("t_shut", "Shutdown")
    kb.events.append(FakeEvent(11, "t_shut", "completed"))

    # Simulate an interrupted tick: claim, then stop before finalizing.
    # _collect_board_claims registers each claim itself (right after the
    # cursor CAS), so appending the batch again here would build a
    # production-impossible double registration (TARS re-review 2026-07-27).
    poller._INFLIGHT_CLAIMS.clear()
    claims = poller._collect_board_claims(kb, "default")
    assert claims and sub.last_event_id == 11
    assert len(poller._INFLIGHT_CLAIMS) == 1, "claim registers exactly once"
    monkeypatch.setattr(poller, "_THREAD", None)

    poller.stop_kanban_notify_poller(timeout=0)

    assert sub.last_event_id == 0, "shutdown must rewind the claimed cursor"
    assert kb.rewinds == ["t_shut"], "rewound exactly once, not twice"
    assert not poller._INFLIGHT_CLAIMS
    assert not [e for e in kb.appended_events if e.kind == "notify_delivery_failed"], (
        "a plain shutdown rewind must not dead-letter anything"
    )


def test_dropping_subscription_rewinds_and_dead_letters(fake_env):
    """Giving up on a channel must not consume the task's events silently."""
    kb, turns, responses = fake_env
    sub = FakeSub("t_dead", "gone")
    kb.subs.append(sub)
    kb.tasks["t_dead"] = FakeTask("t_dead", "Weg")
    kb.events.append(FakeEvent(3, "t_dead", "completed"))
    responses.append({"_status": 404, "error": "Session not found"})

    assert poller.run_tick() == 0
    assert kb.removed == ["t_dead"]
    # Cursor rewound (events not consumed) and the loss recorded on the task.
    assert sub.last_event_id == 0 or ("t_dead" in kb.rewinds)
    assert any(e.kind == "notify_delivery_failed" for e in kb.appended_events), \
        "an undeliverable notification must leave a durable marker"
    marker = next(e for e in kb.appended_events if e.kind == "notify_delivery_failed")
    assert marker.payload["reason"] == "session_not_found"


def test_lost_rewind_race_is_dead_lettered(fake_env):
    """A REAL interleaving: a sibling poller claims past us mid-turn.

    Sequence: we claim [0->4] and start the turn; while that turn is being
    started a second poller claims [4->9]; our turn then fails with 409. Our
    CAS rewind must refuse (the row no longer reads 4), and the loss must be
    recorded instead of silently dropped.
    """
    kb, turns, responses = fake_env
    sub = FakeSub("t_race", "sess1")
    kb.subs.append(sub)
    kb.tasks["t_race"] = FakeTask("t_race", "Race")
    kb.events.append(FakeEvent(4, "t_race", "completed"))
    responses.append({"_status": 409, "error": "session already has an active stream"})

    def sibling_poller_claims_during_turn(session_id, message):
        # Runs at exactly the moment our turn start is in flight.
        kb.events.append(FakeEvent(9, "t_race", "completed"))
        old, new_cursor, evs = kb.claim_unseen_events_for_sub(
            None, task_id="t_race", platform="webui", chat_id="sess1",
        )
        assert (old, new_cursor) == (4, 9), (old, new_cursor)
        turns.append((session_id, message))
        return responses.pop(0)

    import api.kanban_notify_poller as p
    p._start_turn = sibling_poller_claims_during_turn
    try:
        poller.run_tick()
    finally:
        pass

    # The sibling's cursor (9) must survive — our rewind must NOT clobber it.
    assert sub.last_event_id == 9
    assert "t_race" not in kb.rewinds
    assert any(e.kind == "notify_delivery_failed"
               and e.payload["reason"] == "rewind_lost_race"
               for e in kb.appended_events)


def test_claim_is_registered_before_delivery(fake_env):
    """The claim must be shutdown-recoverable the moment the cursor moves."""
    kb, _turns, _ = fake_env
    kb.subs.append(FakeSub("t_reg", "sess1"))
    kb.tasks["t_reg"] = FakeTask("t_reg", "Register")
    kb.events.append(FakeEvent(5, "t_reg", "completed"))

    poller._INFLIGHT_CLAIMS.clear()
    claims = poller._collect_board_claims(kb, "default")
    assert claims
    # Registered by _collect_board_claims itself, not only later in run_tick.
    registered = [c for _kb, batch in poller._INFLIGHT_CLAIMS for c in batch]
    assert [c["sub"]["task_id"] for c in registered] == ["t_reg"]
    poller._INFLIGHT_CLAIMS.clear()


def test_subscription_survives_failed_dead_letter(fake_env, monkeypatch):
    """No durable marker → keep the subscription rather than lose the result."""
    kb, _turns, responses = fake_env
    kb.subs.append(FakeSub("t_nomark", "gone"))
    kb.tasks["t_nomark"] = FakeTask("t_nomark", "Ohne Marker")
    kb.events.append(FakeEvent(6, "t_nomark", "completed"))
    responses.append({"_status": 404, "error": "Session not found"})

    def failing_append(*a, **kw):
        raise RuntimeError("event store unavailable")
    monkeypatch.setattr(kb, "_append_event", failing_append)
    # add_comment fallback must fail too, so no marker can be written.
    monkeypatch.setattr(kb, "add_comment", failing_append, raising=False)

    poller.run_tick()
    assert kb.removed == [], "subscription must survive a failed dead-letter"


def test_stop_does_not_rewind_while_thread_runs(fake_env, monkeypatch):
    """Rewinding under a live tick would race it; leave the claims alone."""
    kb, _turns, _ = fake_env
    sub = FakeSub("t_live", "sess1")
    kb.subs.append(sub)
    kb.tasks["t_live"] = FakeTask("t_live", "Live")
    kb.events.append(FakeEvent(8, "t_live", "completed"))

    poller._INFLIGHT_CLAIMS.clear()
    claims = poller._collect_board_claims(kb, "default")
    assert claims and sub.last_event_id == 8

    class _AliveThread:
        def is_alive(self):
            return True
        def join(self, timeout=None):
            return None

    monkeypatch.setattr(poller, "_THREAD", _AliveThread())
    poller.stop_kanban_notify_poller(timeout=0)

    assert sub.last_event_id == 8, "must not rewind under a running tick"
    assert poller._INFLIGHT_CLAIMS, "claims stay registered for later"
    poller._INFLIGHT_CLAIMS.clear()
