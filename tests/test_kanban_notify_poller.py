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
    active: bool = True
    generation: int = 1
    lease_owner: str | None = None
    lease_until: int | None = None
    lease_version: int = 0

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
    commits: list = field(default_factory=list)
    releases: list = field(default_factory=list)
    clock: int = 1_000_000

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

    # --- Subscription lease (B0, 2026-07-28) -----------------------------
    # Modelled faithfully, fences included: the tests must be able to fail on a
    # missing fence, otherwise they prove nothing about the protocol.

    def acquire_notify_sub_lease(self, conn, *, task_id, platform, chat_id,
                                 thread_id=None, owner, lease_seconds=300,
                                 now=None):
        now = int(self.clock if now is None else now)
        sub = self._sub(task_id, chat_id)
        if sub is None or not sub.active:
            return None
        if sub.lease_until is not None and sub.lease_until > now:
            return None          # a live lease belongs to someone else
        sub.lease_owner = owner
        sub.lease_until = now + lease_seconds
        sub.lease_version += 1
        return {
            "generation": sub.generation,
            "lease_version": sub.lease_version,
            "last_event_id": sub.last_event_id,
        }

    def _fence_ok(self, sub, owner, generation, lease_version):
        return (sub is not None
                and sub.lease_owner == owner
                and sub.lease_version == lease_version
                and sub.generation == generation)

    def commit_notify_sub_delivery(self, conn, *, task_id, platform, chat_id,
                                   thread_id=None, owner, generation,
                                   lease_version, new_cursor):
        sub = self._sub(task_id, chat_id)
        if not self._fence_ok(sub, owner, generation, lease_version):
            return False
        sub.last_event_id = new_cursor
        sub.lease_owner = None
        sub.lease_until = None
        self.commits.append((task_id, new_cursor))
        return True

    def release_notify_sub_lease(self, conn, *, task_id, platform, chat_id,
                                 thread_id=None, owner, generation,
                                 lease_version, retry_after_seconds=0, now=None):
        now = int(self.clock if now is None else now)
        sub = self._sub(task_id, chat_id)
        if not self._fence_ok(sub, owner, generation, lease_version):
            return False
        sub.lease_owner = None
        sub.lease_until = now + retry_after_seconds if retry_after_seconds > 0 else None
        self.releases.append((task_id, retry_after_seconds))
        return True

    def retire_notify_sub_with_marker(self, conn, *, task_id, platform, chat_id,
                                      thread_id=None, owner, generation,
                                      lease_version, reason, payload=None):
        sub = self._sub(task_id, chat_id)
        if not self._fence_ok(sub, owner, generation, lease_version):
            return False
        # Atomic like the real one: if the marker cannot be written, the
        # subscription must NOT end up deactivated without a trace.
        marker = dict(payload or {})
        marker.setdefault("reason", reason)
        self._append_event(conn, task_id, "notify_delivery_failed", marker)
        sub.active = False
        sub.lease_owner = None
        sub.lease_until = None
        self.subs.remove(sub)
        self.removed.append(task_id)
        return True

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


def test_busy_session_parks_without_moving_the_cursor(fake_env):
    """409: the events stay unconsumed — there is nothing to rewind."""
    kb, turns, responses = fake_env
    sub = FakeSub("t_bsy", "sess1")
    kb.subs.append(sub)
    kb.tasks["t_bsy"] = FakeTask("t_bsy", "Beschäftigt", status="running")
    kb.events.append(FakeEvent(5, "t_bsy", "blocked"))
    responses.append({"_status": 409, "error": "session already has an active stream"})

    assert poller.run_tick() == 0
    assert sub.last_event_id == 0, "the cursor never moved in the first place"
    assert not kb.commits
    assert kb.releases and kb.releases[-1][0] == "t_bsy"
    assert kb.releases[-1][1] > 0, "parked with a durable backoff, not free"
    assert sub.lease_owner is None


def test_deleted_session_retires_subscription_with_marker(fake_env):
    """404: deactivation and its evidence land together."""
    kb, _turns, responses = fake_env
    kb.subs.append(FakeSub("t_del", "gone"))
    kb.tasks["t_del"] = FakeTask("t_del", "Weg")
    kb.events.append(FakeEvent(3, "t_del", "completed"))
    responses.append({"_status": 404, "error": "Session not found"})

    assert poller.run_tick() == 0
    assert kb.removed == ["t_del"]
    marker = next(e for e in kb.appended_events if e.kind == "notify_delivery_failed")
    assert marker.payload["reason"] == "session_not_found"
    assert not kb.commits, "an undelivered event must not consume the cursor"


def test_paused_wakeups_park_long(fake_env):
    kb, _turns, responses = fake_env
    sub = FakeSub("t_pause", "sess1")
    kb.subs.append(sub)
    kb.tasks["t_pause"] = FakeTask("t_pause", "Pausiert")
    kb.events.append(FakeEvent(2, "t_pause", "completed"))
    responses.append({"_status": 409, "error": "process_wakeup_paused"})

    poller.run_tick()
    assert sub.last_event_id == 0
    assert kb.releases[-1][1] >= poller._PAUSED_BACKOFF_SECONDS


def test_repeated_failures_cap_and_retire(fake_env):
    kb, turns, responses = fake_env
    kb.subs.append(FakeSub("t_bad", "sess1"))
    kb.tasks["t_bad"] = FakeTask("t_bad", "Kaputt")
    kb.events.append(FakeEvent(4, "t_bad", "completed"))

    for _ in range(poller._MAX_CONSECUTIVE_FAILURES):
        responses.append({"_status": 500})
        poller._BACKOFF_UNTIL.clear()
        # The backoff is DURABLE now: parking the lease is what holds the
        # subscription back, so the in-memory map alone no longer unblocks a
        # retry. Advancing the clock past the park window is the honest way to
        # drive the cap — and it documents that the backoff survives a restart.
        kb.clock += 10_000
        poller.run_tick()

    assert kb.removed == ["t_bad"]
    assert len(turns) == poller._MAX_CONSECUTIVE_FAILURES
    assert any(e.kind == "notify_delivery_failed" for e in kb.appended_events)


# ---------------------------------------------------------------------------
# B0 (2026-07-28): the lease IS the protocol. These replace the rewind tests —
# the pre-B0 design advanced the cursor first and tried to undo it afterwards,
# which a crash took with it.
# ---------------------------------------------------------------------------

def test_lease_is_taken_before_delivery_and_cursor_stays_put(fake_env):
    """The decisive invariant: nothing is consumed before it is delivered."""
    kb, _turns, _ = fake_env
    sub = FakeSub("t_lease", "sess1")
    kb.subs.append(sub)
    kb.tasks["t_lease"] = FakeTask("t_lease", "Lease")
    kb.events.append(FakeEvent(7, "t_lease", "completed"))

    claims = poller._collect_board_claims(kb, "default")
    assert [c["sub"]["task_id"] for c in claims] == ["t_lease"]
    assert sub.last_event_id == 0, "claiming must NOT move the cursor any more"
    assert sub.lease_owner == poller._OWNER
    assert sub.lease_until is not None


def test_crash_between_lease_and_delivery_loses_nothing(fake_env):
    """Simulates the case that made the 27.07. loss unrecoverable."""
    kb, turns, _ = fake_env
    sub = FakeSub("t_crash", "sess1")
    kb.subs.append(sub)
    kb.tasks["t_crash"] = FakeTask("t_crash", "Absturz")
    kb.events.append(FakeEvent(9, "t_crash", "completed"))

    # Tick 1: lease taken, process dies before delivering.
    poller._collect_board_claims(kb, "default")
    assert sub.last_event_id == 0

    # The dead owner's lease expires; a fresh poller generation takes over.
    kb.clock += 10_000
    assert poller.run_tick() == 1, "the events are still there to deliver"
    assert sub.last_event_id == 9, "and NOW the cursor moves"
    assert len(turns) == 1


def test_second_poller_cannot_take_a_live_lease(fake_env):
    """Two WebUI processes: exactly one delivers."""
    kb, _turns, _ = fake_env
    kb.subs.append(FakeSub("t_two", "sess1"))
    kb.tasks["t_two"] = FakeTask("t_two", "Zwei")
    kb.events.append(FakeEvent(11, "t_two", "completed"))

    first = poller._collect_board_claims(kb, "default")
    second = poller._collect_board_claims(kb, "default")
    assert len(first) == 1
    assert second == [], "the live lease belongs to the first caller"


def test_commit_is_fenced_against_a_stale_owner(fake_env):
    """An expired owner must not consume events someone else now owns."""
    kb, _turns, _ = fake_env
    sub = FakeSub("t_fence", "sess1")
    kb.subs.append(sub)
    kb.tasks["t_fence"] = FakeTask("t_fence", "Zaun")
    kb.events.append(FakeEvent(13, "t_fence", "completed"))

    claims = poller._collect_board_claims(kb, "default")
    stale = claims[0]["lease"]

    # Lease expires, another process takes over (bumping lease_version).
    kb.clock += 10_000
    taken = kb.acquire_notify_sub_lease(
        None, task_id="t_fence", platform="webui", chat_id="sess1", owner="other",
    )
    assert taken is not None

    assert kb.commit_notify_sub_delivery(
        None, task_id="t_fence", platform="webui", chat_id="sess1",
        owner=poller._OWNER, generation=stale["generation"],
        lease_version=stale["lease_version"], new_cursor=13,
    ) is False
    assert sub.last_event_id == 0, "the fence protected the new owner's work"


def test_resubscribe_generation_blocks_an_old_lease(fake_env):
    """ABA: unsubscribe + re-subscribe must not accept the old holder."""
    kb, _turns, _ = fake_env
    sub = FakeSub("t_aba", "sess1")
    kb.subs.append(sub)
    kb.tasks["t_aba"] = FakeTask("t_aba", "ABA")
    kb.events.append(FakeEvent(15, "t_aba", "completed"))

    claims = poller._collect_board_claims(kb, "default")
    lease = claims[0]["lease"]

    sub.generation += 1          # re-subscribed in between
    assert kb.commit_notify_sub_delivery(
        None, task_id="t_aba", platform="webui", chat_id="sess1",
        owner=poller._OWNER, generation=lease["generation"],
        lease_version=lease["lease_version"], new_cursor=15,
    ) is False
    assert sub.last_event_id == 0


def test_empty_read_under_lease_releases_it_immediately(fake_env):
    """No events after all: hand the lease back, do not park the subscription."""
    kb, _turns, _ = fake_env
    sub = FakeSub("t_empty", "sess1")
    kb.subs.append(sub)
    kb.tasks["t_empty"] = FakeTask("t_empty", "Leer")
    # No events at all -> the pre-filter skips it; force the lease path by
    # giving it an event the filter sees and removing it before the re-read.
    kb.events.append(FakeEvent(17, "t_empty", "completed"))
    real_unseen = kb.unseen_events_for_sub
    calls = {"n": 0}

    def vanishing(conn, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_unseen(conn, **kw)
        return 0, []                      # gone by the time we hold the lease

    kb.unseen_events_for_sub = vanishing
    assert poller._collect_board_claims(kb, "default") == []
    assert sub.lease_owner is None, "the lease must not be left dangling"
    assert sub.lease_until is None, "and it is free again, not parked"
    assert sub.last_event_id == 0


def test_subscription_survives_a_failed_marker(fake_env, monkeypatch):
    """Terminal state and its evidence are one transaction — or neither."""
    kb, _turns, responses = fake_env
    sub = FakeSub("t_nomark", "gone")
    kb.subs.append(sub)
    kb.tasks["t_nomark"] = FakeTask("t_nomark", "Ohne Marker")
    kb.events.append(FakeEvent(6, "t_nomark", "completed"))
    responses.append({"_status": 404, "error": "Session not found"})

    def failing_append(*a, **kw):
        raise RuntimeError("event store unavailable")
    monkeypatch.setattr(kb, "_append_event", failing_append)

    poller.run_tick()
    assert kb.removed == [], "no marker -> no silent deactivation"
    assert sub in kb.subs
    assert sub.last_event_id == 0, "and the events remain deliverable"


def test_silent_kinds_consume_the_cursor_without_waking(fake_env):
    kb, turns, _ = fake_env
    sub = FakeSub("t_sil2", "sess1")
    kb.subs.append(sub)
    kb.tasks["t_sil2"] = FakeTask("t_sil2", "Leise", status="running")
    kb.events.append(FakeEvent(3, "t_sil2", "status", payload={"status": "running"}))

    assert poller.run_tick() == 0
    assert not turns
    assert sub.last_event_id == 3, "consumed deliberately, not lost"
    assert sub.lease_owner is None
