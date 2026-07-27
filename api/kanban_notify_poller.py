"""Kanban notification poller: deliver task events into WebUI sessions.

``kanban_create`` auto-subscribes the creating session to its task's terminal
events (``kanban_notify_subs`` rows with ``platform='webui'``, ``chat_id`` =
WebUI session id). The gateway notifier cannot serve these rows — ``webui`` is
not a messaging platform in its ``Platform`` enum, so it skips them before the
event claim and the subscription cursor never moves. Until this poller existed,
``subscribed: true`` from ``kanban_create`` was a promise nobody kept: task
results piled up in the kanban DB and the session that ordered the work was
never told.

This poller is the WebUI-side consumer. Per tick it scans every board DB for
``platform='webui'`` subscriptions, claims unseen terminal events with the same
CAS discipline the gateway uses (``claim_unseen_events_for_sub`` /
``rewind_notify_cursor``), and delivers them by starting a server-side agent
turn (``api.routes.start_session_turn``) — the same Option-Z path background
process completions use. The turn both shows the notification in the session
transcript and lets the agent act on the result (read artifacts, present the
outcome), which a passive UI ping could not.

Delivery contract:
  - The subscription cursor is the durable queue. Claim advances it; any
    delivery failure rewinds it (CAS-guarded), so events survive process
    restarts. A crash between claim and turn-start loses that batch — the same
    documented trade-off the gateway notifier makes.
  - One turn per session per tick: all deliverable events for a session are
    batched into a single wakeup prompt, so parallel claims cannot race each
    other into 409s.
  - ``status`` / ``archived`` / ``unblocked`` events are claimed but dropped
    silently: they must not wake the agent, but leaving them unclaimed would
    park the cursor in front of a later ``completed`` forever.
  - ``source="process_wakeup"`` deliberately opts into the provider-credential
    circuit breaker in ``start_session_turn`` — a poller that can wake dozens
    of sessions must not hammer an exhausted credential pool.

The poller must run inside the WebUI server process (session JSON writes are
only serialized per-process); it degrades to a no-op when ``hermes_cli`` is not
installed, mirroring the kanban bridge's 503 behaviour.
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger("hermes_webui.kanban_notify")

# Mirrors gateway/kanban_watchers.py TERMINAL_KINDS: everything here must be
# CLAIMED so no unclaimed row blocks the cursor, but only _DELIVER_KINDS may
# wake the agent.
CLAIM_KINDS = (
    "completed", "blocked", "gave_up", "crashed", "timed_out",
    "status", "archived", "unblocked",
)
_DELIVER_KINDS = frozenset({"completed", "blocked", "gave_up", "crashed", "timed_out"})

_KIND_ICONS = {
    "completed": "✔",  # ✔
    "blocked": "⏸",    # ⏸
    "gave_up": "✖",    # ✖
    "crashed": "✖",    # ✖
    "timed_out": "⏱",  # ⏱
}

_STOP = threading.Event()
_THREAD: threading.Thread | None = None
_LIFECYCLE_LOCK = threading.Lock()

# Per-session in-memory retry state. Deliberately volatile: after a restart the
# durable cursor makes every undelivered event eligible again, which is the
# correct recovery behaviour.
_BACKOFF_UNTIL: dict[str, float] = {}
_FAILURES: dict[str, int] = {}

_RETRY_BACKOFF_SECONDS = 30.0        # active turn raced us; try again soon
_PAUSED_BACKOFF_SECONDS = 600.0      # provider credential pool exhausted
_FAILURE_BACKOFF_SECONDS = 120.0     # 400/500-class turn-start failures
_MAX_CONSECUTIVE_FAILURES = 5        # then drop the subscription (gateway: 3)


def _enabled() -> bool:
    return os.environ.get("HERMES_WEBUI_KANBAN_NOTIFY", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _interval_seconds() -> float:
    raw = os.environ.get("HERMES_WEBUI_KANBAN_NOTIFY_INTERVAL", "").strip()
    try:
        val = float(raw) if raw else 5.0
    except ValueError:
        val = 5.0
    return max(1.0, val)


def _kanban_db():
    """Import hermes_cli.kanban_db via the bridge's lazy accessor, or None."""
    try:
        from api.kanban_bridge import _kb
        return _kb()
    except Exception:
        return None


def _board_slugs(kb) -> list[str]:
    """All non-archived board slugs, deduped by resolved DB path.

    Mirrors the gateway notifier: two slugs resolving to the same file must
    not be polled twice per tick.
    """
    slugs: list[str] = []
    seen_paths: set[str] = set()
    try:
        boards = kb.list_boards(include_archived=False)
    except Exception:
        logger.warning("kanban notify: list_boards failed", exc_info=True)
        return []
    for meta in boards or []:
        slug = (meta.get("slug") if isinstance(meta, dict) else None) or "default"
        try:
            path = str(kb.kanban_db_path(board=slug))
        except Exception:
            path = slug
        real = os.path.realpath(path)
        if real in seen_paths:
            continue
        seen_paths.add(real)
        slugs.append(slug)
    return slugs


def _collect_board_claims(kb, slug: str) -> list[dict]:
    """Claim unseen events for every webui subscription of one board.

    Returns claim records; the connection is closed before any delivery work
    so no SQLite handle is held across a turn start.
    """
    from api.kanban_bridge import _conn

    claims: list[dict] = []
    now = time.monotonic()
    with _conn(slug) as conn:
        try:
            subs = kb.list_notify_subs(conn)
        except Exception:
            logger.warning("kanban notify: list_notify_subs failed for board %s",
                           slug, exc_info=True)
            return []
        for sub in subs:
            if (sub.get("platform") or "").lower() != "webui":
                continue
            chat_id = str(sub.get("chat_id") or "")
            if not chat_id:
                continue
            if _BACKOFF_UNTIL.get(chat_id, 0.0) > now:
                continue
            # Cheap read-only pre-filter so 100+ idle subscriptions don't take
            # a write lock every tick just to learn there is nothing new.
            try:
                _, pending = kb.unseen_events_for_sub(
                    conn,
                    task_id=sub["task_id"],
                    platform=sub["platform"],
                    chat_id=sub["chat_id"],
                    thread_id=sub.get("thread_id") or None,
                    kinds=CLAIM_KINDS,
                )
            except Exception:
                logger.warning("kanban notify: unseen_events_for_sub failed for %s",
                               sub.get("task_id"), exc_info=True)
                continue
            if not pending:
                continue
            try:
                old_cursor, new_cursor, events = kb.claim_unseen_events_for_sub(
                    conn,
                    task_id=sub["task_id"],
                    platform=sub["platform"],
                    chat_id=sub["chat_id"],
                    thread_id=sub.get("thread_id") or None,
                    kinds=CLAIM_KINDS,
                )
            except Exception:
                logger.warning("kanban notify: claim failed for %s",
                               sub.get("task_id"), exc_info=True)
                continue
            if not events:
                continue
            task = None
            try:
                task = kb.get_task(conn, sub["task_id"])
            except Exception:
                logger.debug("kanban notify: get_task failed for %s",
                             sub.get("task_id"), exc_info=True)
            claims.append({
                "board": slug,
                "sub": dict(sub),
                "old_cursor": old_cursor,
                "new_cursor": new_cursor,
                "events": events,
                "task": task,
            })
    return claims


def _event_line(claim: dict, ev) -> str | None:
    kind = getattr(ev, "kind", None)
    if kind not in _DELIVER_KINDS:
        return None
    sub = claim["sub"]
    task = claim["task"]
    board = claim["board"]
    board_tag = f"[{board}] " if board and board != "default" else ""
    title = (task.title if task is not None else sub["task_id"]) or ""
    title = title[:120]
    icon = _KIND_ICONS.get(kind, "•")
    payload = getattr(ev, "payload", None) or {}

    if kind == "completed":
        # Prefer the run's handoff summary, fall back to task.result — same
        # precedence as the gateway notifier.
        handoff = ""
        summary = str(payload.get("summary") or "").strip()
        if summary:
            handoff = "\n  " + summary.splitlines()[0][:200]
        elif task is not None and getattr(task, "result", None):
            handoff = "\n  " + str(task.result).strip().splitlines()[0][:160]
        return f"{icon} {board_tag}Kanban {sub['task_id']} done — {title}{handoff}"
    if kind == "blocked":
        reason = str(payload.get("reason") or "").strip()[:160]
        suffix = f": {reason}" if reason else ""
        return f"{icon} {board_tag}Kanban {sub['task_id']} blocked — {title}{suffix}"
    if kind == "gave_up":
        err = str(payload.get("error") or "").strip()[:200]
        suffix = f"\n  {err}" if err else ""
        return (f"{icon} {board_tag}Kanban {sub['task_id']} gave up after repeated "
                f"failures — {title}{suffix}")
    if kind == "crashed":
        return (f"{icon} {board_tag}Kanban {sub['task_id']} worker crashed — {title} "
                f"(dispatcher will retry)")
    if kind == "timed_out":
        return f"{icon} {board_tag}Kanban {sub['task_id']} timed out — {title} (will retry)"
    return None


def _format_wakeup(lines: list[str]) -> str:
    """[IMPORTANT: …] wakeup prompt, following format_wakeup_prompt's shape."""
    body = "\n".join(lines)
    return (
        "[IMPORTANT: Kanban task notification (system-generated, not typed by "
        "the user).\n"
        f"{body}\n"
        "Review each finished task (kanban_show <id>, read its durable "
        "artifacts) and deliver the outcome to the user in this session now. "
        "A stored artifact is not a delivered result. For blocked or failed "
        "tasks, state the blocker and what you will do about it.]"
    )


def _start_turn(session_id: str, message: str) -> dict:
    """Indirection over api.routes.start_session_turn (patchable in tests)."""
    from api.routes import start_session_turn
    return start_session_turn(session_id, message, source="process_wakeup")


def _finalize_claims(kb, claims: list[dict], *, delivered: bool) -> None:
    """Rewind cursors on failure; drop subscriptions of finished tasks on success."""
    by_board: dict[str, list[dict]] = {}
    for c in claims:
        by_board.setdefault(c["board"], []).append(c)
    from api.kanban_bridge import _conn

    for board, board_claims in by_board.items():
        try:
            with _conn(board) as conn:
                for c in board_claims:
                    sub = c["sub"]
                    if not delivered:
                        try:
                            kb.rewind_notify_cursor(
                                conn,
                                task_id=sub["task_id"],
                                platform=sub["platform"],
                                chat_id=sub["chat_id"],
                                thread_id=sub.get("thread_id") or None,
                                claimed_cursor=c["new_cursor"],
                                old_cursor=c["old_cursor"],
                            )
                        except Exception:
                            logger.warning("kanban notify: rewind failed for %s",
                                           sub.get("task_id"), exc_info=True)
                        continue
                    task = c["task"]
                    if task is not None and getattr(task, "status", None) in ("done", "archived"):
                        try:
                            kb.remove_notify_sub(
                                conn,
                                task_id=sub["task_id"],
                                platform=sub["platform"],
                                chat_id=sub["chat_id"],
                                thread_id=sub.get("thread_id") or None,
                            )
                        except Exception:
                            logger.debug("kanban notify: unsubscribe failed for %s",
                                         sub.get("task_id"), exc_info=True)
        except Exception:
            logger.warning("kanban notify: finalize failed for board %s",
                           board, exc_info=True)


def _drop_subscriptions(kb, claims: list[dict]) -> None:
    """Deactivate subscriptions whose destination can never be served."""
    by_board: dict[str, list[dict]] = {}
    for c in claims:
        by_board.setdefault(c["board"], []).append(c)
    from api.kanban_bridge import _conn

    for board, board_claims in by_board.items():
        try:
            with _conn(board) as conn:
                for c in board_claims:
                    sub = c["sub"]
                    try:
                        kb.remove_notify_sub(
                            conn,
                            task_id=sub["task_id"],
                            platform=sub["platform"],
                            chat_id=sub["chat_id"],
                            thread_id=sub.get("thread_id") or None,
                        )
                    except Exception:
                        logger.debug("kanban notify: drop sub failed for %s",
                                     sub.get("task_id"), exc_info=True)
        except Exception:
            logger.warning("kanban notify: drop subs failed for board %s",
                           board, exc_info=True)


def run_tick() -> int:
    """One poll cycle. Returns the number of sessions a turn was started for.

    Split out of the loop for tests and for one-shot draining.
    """
    kb = _kanban_db()
    if kb is None:
        return 0

    claims: list[dict] = []
    for slug in _board_slugs(kb):
        try:
            claims.extend(_collect_board_claims(kb, slug))
        except Exception:
            logger.warning("kanban notify: board %s tick failed", slug, exc_info=True)
    if not claims:
        return 0

    by_session: dict[str, list[dict]] = {}
    for c in claims:
        by_session.setdefault(str(c["sub"]["chat_id"]), []).append(c)

    started = 0
    for session_id, session_claims in by_session.items():
        lines: list[str] = []
        for c in session_claims:
            for ev in c["events"]:
                line = _event_line(c, ev)
                if line:
                    lines.append(line)
        if not lines:
            # Only silent kinds (status/archived/unblocked): consume without
            # waking anyone, and still drop subs of finished tasks.
            _finalize_claims(kb, session_claims, delivered=True)
            continue

        try:
            resp = _start_turn(session_id, _format_wakeup(lines)) or {}
        except Exception:
            logger.warning("kanban notify: start_session_turn raised for %s",
                           session_id, exc_info=True)
            resp = {"_status": 500}
        status = int(resp.get("_status", 200) or 200)

        if status == 200:
            _FAILURES.pop(session_id, None)
            _BACKOFF_UNTIL.pop(session_id, None)
            _finalize_claims(kb, session_claims, delivered=True)
            started += 1
            logger.info("kanban notify: woke session %s with %d event(s)",
                        session_id, len(lines))
        elif status == 404:
            # Session is gone; the subscription can never deliver. Drop it so
            # the notifier stops claiming for a dead destination.
            _drop_subscriptions(kb, session_claims)
            logger.info("kanban notify: session %s not found; dropped %d subscription(s)",
                        session_id, len(session_claims))
        elif status == 409 and resp.get("error") == "process_wakeup_paused":
            _BACKOFF_UNTIL[session_id] = time.monotonic() + _PAUSED_BACKOFF_SECONDS
            _finalize_claims(kb, session_claims, delivered=False)
            logger.info("kanban notify: session %s wakeups paused; backing off %.0fs",
                        session_id, _PAUSED_BACKOFF_SECONDS)
        elif status == 409:
            # An active turn raced us. The durable cursor is our redelivery
            # queue: rewind and retry after a short backoff.
            _BACKOFF_UNTIL[session_id] = time.monotonic() + _RETRY_BACKOFF_SECONDS
            _finalize_claims(kb, session_claims, delivered=False)
            logger.debug("kanban notify: session %s busy; will retry", session_id)
        else:
            failures = _FAILURES.get(session_id, 0) + 1
            _FAILURES[session_id] = failures
            if failures >= _MAX_CONSECUTIVE_FAILURES:
                _drop_subscriptions(kb, session_claims)
                _FAILURES.pop(session_id, None)
                _BACKOFF_UNTIL.pop(session_id, None)
                logger.warning(
                    "kanban notify: session %s failed %d times (status=%s); "
                    "dropping %d subscription(s)",
                    session_id, failures, status, len(session_claims),
                )
            else:
                _BACKOFF_UNTIL[session_id] = time.monotonic() + _FAILURE_BACKOFF_SECONDS
                _finalize_claims(kb, session_claims, delivered=False)
                logger.warning(
                    "kanban notify: turn start failed for session %s "
                    "(status=%s, attempt %d/%d)",
                    session_id, status, failures, _MAX_CONSECUTIVE_FAILURES,
                )
    return started


def _poll_loop() -> None:
    if _kanban_db() is None:
        logger.info("kanban notify poller unavailable: hermes_cli not importable")
        return
    interval = _interval_seconds()
    logger.info("kanban notify poller started (interval=%.1fs)", interval)
    # Same startup grace the gateway notifier uses: let the server finish
    # binding routes before the first turn could possibly start.
    _STOP.wait(5.0)
    while not _STOP.is_set():
        try:
            run_tick()
        except Exception:
            logger.warning("kanban notify tick failed", exc_info=True)
        _STOP.wait(interval)


def start_kanban_notify_poller() -> bool:
    """Start the poller thread idempotently. Returns True on first start."""
    global _THREAD
    if not _enabled():
        logger.info("kanban notify poller disabled via HERMES_WEBUI_KANBAN_NOTIFY")
        return False
    with _LIFECYCLE_LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return False
        _STOP.clear()
        _THREAD = threading.Thread(
            target=_poll_loop,
            name="hermes-webui-kanban-notify",
            daemon=True,
        )
        _THREAD.start()
        return True


def stop_kanban_notify_poller(timeout: float = 2.0) -> None:
    _STOP.set()
    th = _THREAD
    if th is not None and th.is_alive():
        th.join(timeout=timeout)
