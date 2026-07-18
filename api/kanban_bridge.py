"""Hermes Kanban bridge for the WebUI.

This module exposes a full CRUD API under ``/api/kanban/*`` while keeping
Hermes Agent's ``hermes_cli.kanban_db`` as the only source of truth.

Supported operations:
- Task CRUD (create, read, patch, bulk update, archive)
- Multi-board management (list, create, archive, switch)
- Task dependency links (create, delete)
- SSE live event stream for real-time updates
- Comments and worker dispatch integration
"""

from __future__ import annotations

import json
from api.sse_chunked import end_sse_headers
import time
from dataclasses import asdict, is_dataclass
from urllib.parse import parse_qs, unquote

from api.helpers import bad, j

BOARD_COLUMNS = ["triage", "todo", "ready", "running", "blocked", "done"]
_TASK_PREFIX = "/api/kanban/tasks/"


class KanbanGoneError(RuntimeError):
    """Requested approval existed but is expired, consumed, or cancelled."""


def _kb():
    """Lazily import hermes_cli.kanban_db to avoid circular imports at module load."""
    from hermes_cli import kanban_db as kb

    return kb


def _resolve_board(parsed):
    """Validate and normalise a ?board=<slug> query param.

    Returns the normalised slug, or ``None`` when the caller omitted the
    param. Raises ValueError on a malformed slug so the bridge surfaces a
    clean 400 instead of a 500 from deeper in the library.
    """
    raw = (parse_qs(parsed.query or "").get("board") or [None])[0]
    return _normalise_board_or_raise(raw)


def _resolve_board_from_body(body):
    """Same contract as :func:`_resolve_board` but reads ``board`` from a
    parsed JSON body (POST / PATCH / DELETE handlers receive a dict, not
    a parsed URL). Returns ``None`` when the body did not specify a board.
    """
    if not isinstance(body, dict):
        return None
    raw = body.get("board")
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    return _normalise_board_or_raise(raw)


def _normalise_board_or_raise(raw):
    """Shared normalisation + existence check for board slugs."""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    kb = _kb()
    try:
        normed = kb._normalize_board_slug(raw)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"invalid board slug: {raw!r}") from exc
    if not normed:
        return None
    # Allow the default board even if it has not been materialised yet
    # (kb.init_db will create it lazily). For non-default boards, require
    # the directory exists or _conn would fail with a confusing OperationalError.
    try:
        default_slug = getattr(kb, "DEFAULT_BOARD", "default")
    except Exception:
        default_slug = "default"
    if normed != default_slug and not kb.board_exists(normed):
        raise LookupError(f"board {normed!r} does not exist")
    return normed


def _conn(board=None):
    """Initialize the kanban DB for the given board slug and return a context manager
    that yields a sqlite connection and CLOSES it on exit.

    Must be ``kb.connect_closing`` — a raw ``kb.connect()`` connection used as
    ``with _conn(...) as conn:`` only gets sqlite3's transaction-scope context
    manager, which never closes the file descriptor. In this long-lived server
    that leaks one FD per request and pins stale WAL snapshots (FDs to deleted
    ``-wal``/``-shm`` files), which starves SQLite checkpoints on the shared
    kanban DB and aggravates probe⇄checkpoint contention for every process.
    """
    kb = _kb()
    kb.init_db(board=board)
    closing = getattr(kb, "connect_closing", None)
    if closing is not None:
        return closing(board=board)
    # Older kanban_db builds (and lightweight test doubles) without
    # connect_closing: fall back to the raw connection; sqlite3's own
    # context manager at least scopes the transaction.
    return kb.connect(board=board)


def _obj_dict(value):
    """Coerce a dataclass or arbitrary object to a plain dict; returns None unchanged."""
    if value is None:
        return None
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    return dict(getattr(value, "__dict__", {}))


def _task_dict(task):
    """Convert a task to a JSON-serialisable dict, annotating it with computed age_seconds and progress fields."""
    data = _obj_dict(task)
    if not data:
        return data
    try:
        age = _kb().task_age(task)
    except Exception:
        age = None
    data["age_seconds"] = age
    data["age"] = age
    data.setdefault("progress", None)
    return data


def _safe_exact_action_summary(_value) -> str:
    """Return the only summary permitted across the opaque approval boundary.

    Exact-action rows are normally redacted by Core before persistence, but the
    WebUI must not trust arbitrary historical/event prose or redactor coverage.
    The detailed command stays exclusively in the worker-side approval context.
    """
    return "An exact terminal action is awaiting approval."


def _blocker_details(conn, tasks):
    """Project durable human attention independently of the current column."""
    task_ids = [t.id for t in tasks]
    if not task_ids:
        return {}
    kb = _kb()
    out = {}
    try:
        placeholders = ",".join("?" for _ in task_ids)
        rows = conn.execute(
            "SELECT id, task_id, kind, payload, created_at FROM task_events "
            "WHERE task_id IN (" + placeholders + ") ORDER BY id ASC",
            task_ids,
        ).fetchall()
    except Exception:
        rows = []

    pending_by_task = {}
    if hasattr(kb, "get_pending_action"):
        for task_id in task_ids:
            try:
                action = kb.get_pending_action(conn, task_id)
            except Exception:
                action = None
            if action is not None:
                pending_by_task[task_id] = action

    # get_pending_action does not filter by attention type, so an approved
    # action whose worker was killed on a runtime timeout looks identical here
    # to one a worker is about to run. The Core parks the former by swapping the
    # projection to capability/transient -- the ONLY signal that nothing is
    # coming. Without reading it this view tells the operator "waiting for the
    # resumed worker" forever, about a worker that will never exist.
    attention_by_task = {}
    if hasattr(kb, "get_current_attentions"):
        try:
            attention_by_task = kb.get_current_attentions(conn, task_ids) or {}
        except Exception:
            attention_by_task = {}

    resolution_kinds = {
        "terminal_action_resolved", "terminal_action_completed",
        "pending_action_resolved", "action_completed",
        "terminal_approval_consumed", "terminal_approval_cancelled",
    }
    for row in rows:
        payload = {}
        raw = row["payload"] if "payload" in row.keys() else None
        if raw:
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {}
        task_id = row["task_id"]
        if row["kind"] in resolution_kinds:
            if task_id not in pending_by_task:
                out.pop(task_id, None)
            continue
        if row["kind"] == "unblocked":
            if task_id not in pending_by_task:
                out.pop(task_id, None)
            continue
        if row["kind"] in {"completed", "archived"}:
            out.pop(task_id, None)
            pending_by_task.pop(task_id, None)
            continue
        if row["kind"] not in {"blocked", "block_loop_detected"}:
            continue
        detail = {
            "human_summary": (payload.get("human_summary") or "").strip() or None,
            "human_action": (payload.get("human_action") or "").strip() or None,
            "reason": (payload.get("reason") or "").strip() or None,
            "block_kind": payload.get("kind"),
            "source_event_kind": row["kind"],
        }
        if any((detail["human_summary"], detail["human_action"], detail["reason"])):
            out[task_id] = detail

    core_can_approve = all(callable(getattr(kb, name, None)) for name in (
        "get_pending_action", "get_pending_action_by_id",
        "approve_pending_action_and_unblock",
    ))
    core_can_reject = all(callable(getattr(kb, name, None)) for name in (
        "get_pending_action", "get_pending_action_by_id", "resolve_pending_action",
    ))
    for task_id in task_ids:
        action = pending_by_task.get(task_id)
        detail = out.get(task_id)
        if action is None and detail is None:
            continue
        if detail is None:
            detail = {
                "human_summary": getattr(action, "summary", None),
                "human_action": "Approve the exact terminal action or let it expire.",
                "reason": "exact terminal action approval pending",
                "block_kind": "needs_input",
                "source_event_kind": "terminal_approval_pending",
            }
        sticky = action is not None
        action_approved = bool(
            sticky and getattr(action, "approved_at", None) is not None
        )
        attention = attention_by_task.get(task_id)
        attention_type = getattr(attention, "type", None)
        # An approved action parked by a technical failure: the worker was
        # killed mid-execution (e.g. enforce_max_runtime) and the Core swapped
        # the exact projection for a technical one. Nothing will resume it on
        # its own -- resume_approved_action_retry is the only way out.
        technical_park = bool(
            sticky and action_approved
            and attention_type in ("capability", "transient")
        )
        if sticky:
            # Exact-action transport is opaque, so we never trust the raw action
            # row or arbitrary blocker-EVENT prose (either may carry a raw command
            # or secret). But the ATTENTION is Core's curated *public projection*:
            # its summary is the operator-facing pattern description Core mirrors
            # in on record (_upsert_exact_action_attention). Prefer it so the
            # cockpit can say WHY the card is blocked -- the exact same trust
            # boundary the dashboard plugin uses (plugin_api._attention_dict, which
            # reads attention.summary and never the action). Fall back to the
            # generic constant when no live projection carries a summary.
            _stored = (getattr(attention, "summary", None) or "").strip()
            detail["human_summary"] = _stored or _safe_exact_action_summary(
                getattr(action, "summary", None)
            )
            detail["reason"] = (
                "approved exact action parked after a technical failure"
                if technical_park else "exact terminal action approval pending"
            )
            detail["human_action"] = (
                "Approved, but the worker failed technically and was parked. "
                "Nothing will resume on its own — retry it, or reject the action."
                if technical_park else
                "Approved. Waiting for the resumed worker to execute the exact action."
                if action_approved
                else "Approve the exact terminal action or let it expire."
            )
        action_id = getattr(action, "id", None) if action is not None else None
        core_can_resume = callable(getattr(kb, "resume_approved_action_retry", None))
        resume_available = bool(
            technical_park and core_can_resume
            and getattr(attention, "origin_run_id", None) is not None
        )
        detail.update({
            "sticky": sticky,
            "attention_type": attention_type,
            "technical_park": technical_park,
            "pending_action_id": action_id,
            "pending_action_approved": action_approved,
            "approval_unavailable_reason": (
                "core" if sticky and not core_can_approve else None
            ),
            "pending_action_expires_at": (
                getattr(action, "expires_at", None) if action is not None else None
            ),
            "actions": {
                # For a typed non-exact attention the release goes through the
                # Core's CAS seam; the cockpit echoes this pair back on
                # /unblock so the operator resolves the projection they saw.
                "plain_unblock": {
                    "allowed": not sticky,
                    "attention_id": (
                        getattr(attention, "id", None) if not sticky else None
                    ),
                    "attention_version": (
                        getattr(attention, "version", None) if not sticky else None
                    ),
                },
                "approve_exact_action": {
                    "available": bool(
                        sticky and not action_approved
                        and action_id is not None and core_can_approve
                    ),
                    "endpoint": (
                        "approve-exact-action"
                        if sticky and not action_approved
                        and action_id is not None and core_can_approve else None
                    ),
                },
                # Rejecting stays available on an ALREADY-APPROVED action too:
                # an approved-but-unconsumed grant is exactly the state an
                # operator may want to take back, and until it is settled the
                # card cannot be unblocked either.
                "reject_exact_action": {
                    "available": bool(
                        sticky and action_id is not None and core_can_reject
                    ),
                    "endpoint": (
                        "reject-exact-action"
                        if sticky and action_id is not None and core_can_reject else None
                    ),
                },
                # The escape from a technical park. Carries the CAS handles the
                # Core seam demands, because the operator cannot invent them.
                "resume_approved_action_retry": {
                    "available": resume_available,
                    "endpoint": "resume-approved-action-retry" if resume_available else None,
                    "attention_id": getattr(attention, "id", None) if resume_available else None,
                    "attention_version": getattr(attention, "version", None) if resume_available else None,
                    "origin_run_id": getattr(attention, "origin_run_id", None) if resume_available else None,
                },
            },
        })
        out[task_id] = detail
    return out


def _sticky_block_detail(conn, task_id):
    task = _kb().get_task(conn, task_id)
    if not task:
        raise LookupError("task not found")
    return _blocker_details(conn, [task]).get(task_id)


def _latest_event_id(conn) -> int:
    """Return the highest event id in task_events, falling back to 0 when the table is empty."""
    try:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS latest FROM task_events").fetchone()
        return int(row["latest"] or 0)
    except Exception:
        return 0


def _bool_query(parsed, name: str, default: bool = False) -> bool:
    """Extract a boolean query param, treating 1/true/yes/on (case-insensitive) as True."""
    raw = (parse_qs(parsed.query or "").get(name) or [None])[0]
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _str_query(parsed, name: str):
    """Extract a string query param, returning None when the param is absent or blank."""
    raw = (parse_qs(parsed.query or "").get(name) or [None])[0]
    return str(raw).strip() or None if raw is not None else None


def _int_query(parsed, name: str, default=None, *, minimum=None, maximum=None):
    """Extract an integer query param, clamped to [minimum, maximum] when those bounds are provided."""
    raw = _str_query(parsed, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _task_link_counts(conn, tasks):
    """Return a dict mapping each task id to its {parents, children} dependency link counts."""
    counts = {task.id: {"parents": 0, "children": 0} for task in tasks}
    try:
        rows = conn.execute("SELECT parent_id, child_id FROM task_links").fetchall()
    except Exception:
        return counts
    for row in rows:
        counts.setdefault(row["parent_id"], {"parents": 0, "children": 0})["children"] += 1
        counts.setdefault(row["child_id"], {"parents": 0, "children": 0})["parents"] += 1
    return counts


def _comment_counts(conn):
    """Return a dict mapping each task id to its total comment count across the board."""
    try:
        rows = conn.execute(
            "SELECT task_id, COUNT(*) AS n FROM task_comments GROUP BY task_id"
        ).fetchall()
    except Exception:
        return {}
    return {row["task_id"]: int(row["n"] or 0) for row in rows}


def _board_payload(parsed):
    """Build the full board JSON payload: kanban columns with tasks, filter state, and latest_event_id."""
    board = _resolve_board(parsed)
    kb = _kb()
    tenant = _str_query(parsed, "tenant")
    assignee = _str_query(parsed, "assignee")
    include_archived = _bool_query(parsed, "include_archived", False)
    only_mine = _bool_query(parsed, "only_mine", False)
    since = _int_query(parsed, "since", None, minimum=0)
    profile = None
    if only_mine and not assignee:
        try:
            from api.profiles import get_active_profile_name

            profile = get_active_profile_name() or "default"
        except Exception:
            profile = "default"
        assignee = profile

    with _conn(board=board) as conn:
        latest_event_id = _latest_event_id(conn)
        if since is not None and since >= latest_event_id:
            return {"changed": False, "latest_event_id": latest_event_id, "read_only": False}

        tasks = kb.list_tasks(
            conn,
            tenant=tenant,
            assignee=assignee,
            include_archived=include_archived,
        )
        link_counts = _task_link_counts(conn, tasks)
        comment_counts = _comment_counts(conn)
        blocker_details = _blocker_details(conn, tasks)

        def row(task):
            data = _task_dict(task)
            data["link_counts"] = link_counts.get(task.id, {"parents": 0, "children": 0})
            data["comment_count"] = comment_counts.get(task.id, 0)
            detail = blocker_details.get(task.id)
            if detail:
                # Layman-facing block context (human_summary/human_action/reason
                # from the latest 'blocked' event) so the WebUI cockpit can lead
                # with a plain-language card instead of an extra per-task fetch.
                data["block_detail"] = detail
            return data

        columns = [
            {"name": name, "tasks": [row(task) for task in tasks if task.status == name]}
            for name in BOARD_COLUMNS
        ]
        if include_archived:
            columns.append({
                "name": "archived",
                "tasks": [row(task) for task in tasks if task.status == "archived"],
            })
        return {
            "columns": columns,
            "tenants": sorted({task.tenant for task in tasks if getattr(task, "tenant", None)}),
            "assignees": sorted({task.assignee for task in tasks if getattr(task, "assignee", None)}),
            "latest_event_id": latest_event_id,
            "changed": True,
            "read_only": False,
            "filters": {
                "tenant": tenant,
                "assignee": assignee,
                "include_archived": include_archived,
                "only_mine": only_mine,
                "profile": profile,
            },
        }



def _validate_status(status: str) -> str:
    """Validate a status string against BOARD_COLUMNS, raising ValueError for unrecognised values."""
    value = str(status or "").strip().lower()
    allowed = set(BOARD_COLUMNS) | {"archived"}
    if value not in allowed:
        raise ValueError(f"invalid status: {value}")
    return value


def _set_status_direct(conn, task_id: str, new_status: str) -> bool:
    """Direct status write for drag-drop moves not covered by structured verbs.

    Used for ``todo <-> ready`` and ``running -> ready`` transitions. The
    structured verbs (``complete_task``, ``block_task``, ``unblock_task``,
    ``archive_task``, ``claim_task``) own their own state changes; this helper
    handles the remainder while preserving the dispatcher's contract:

    - When transitioning OFF ``running`` to anything other than the terminal
      verbs, claim_lock / claim_expires / worker_pid are nulled so the
      dispatcher doesn't see a phantom-running task. The active run (if any)
      is closed with ``outcome='reclaimed'`` so attempt history isn't
      orphaned.
    - When transitioning INTO ``running``, claim fields are preserved (this
      function is NOT used for entering 'running' — that goes through
      ``kb.claim_task()`` and the bridge rejects raw 'running' status writes
      with HTTP 400).

    Mirrors the agent dashboard plugin's ``_set_status_direct``
    (plugins/kanban/dashboard/plugin_api.py) so first-party clients see
    identical behaviour from either surface.
    """
    kb = _kb()
    with kb.write_txn(conn):
        prev = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if prev is None:
            return False
        was_running = prev["status"] == "running"
        cur = conn.execute(
            "UPDATE tasks SET status = ?, "
            "  claim_lock = CASE WHEN ? = 'running' THEN claim_lock ELSE NULL END, "
            "  claim_expires = CASE WHEN ? = 'running' THEN claim_expires ELSE NULL END, "
            "  worker_pid = CASE WHEN ? = 'running' THEN worker_pid ELSE NULL END "
            "WHERE id = ?",
            (new_status, new_status, new_status, new_status, task_id),
        )
        if cur.rowcount != 1:
            return False
        run_id = None
        if was_running and new_status != "running" and prev["current_run_id"]:
            try:
                run_id = kb._end_run(
                    conn, task_id,
                    outcome="reclaimed", status="reclaimed",
                    summary=f"status changed to {new_status} (webui/direct)",
                )
            except Exception:
                # _end_run is best-effort here; the status flip itself is
                # what matters for sidebar rendering.
                run_id = None
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, 'status', ?, ?)",
            (task_id, run_id, json.dumps({"status": new_status, "source": "webui"}), int(time.time())),
        )
    if new_status in ("done", "ready") and hasattr(kb, "recompute_ready"):
        try:
            kb.recompute_ready(conn)
        except Exception:
            pass
    return True


def _create_task_payload(body: dict, *, board=None):
    """Create a new task from a parsed request body and return the task dict in a read_only envelope."""
    title = str(body.get("title") or "").strip()
    if not title:
        raise ValueError("title is required")
    try:
        priority = int(body.get("priority") or 0)
    except (TypeError, ValueError):
        raise ValueError("priority must be an integer")
    kb = _kb()
    requested_status = body.get("status")
    with _conn(board=board) as conn:
        task_id = kb.create_task(
            conn,
            title=title,
            body=body.get("body") or None,
            assignee=body.get("assignee") or None,
            created_by=body.get("created_by") or "webui",
            tenant=body.get("tenant") or None,
            priority=priority,
            parents=body.get("parents") or (),
            triage=bool(body.get("triage") or False),
            workspace_kind=body.get("workspace_kind") or "scratch",
            workspace_path=body.get("workspace_path") or None,
            idempotency_key=body.get("idempotency_key") or None,
            max_runtime_seconds=body.get("max_runtime_seconds") or None,
            skills=body.get("skills") or None,
        )
        if requested_status:
            _patch_task(conn, task_id, {"status": requested_status})
        return {"task": _task_dict(kb.get_task(conn, task_id)), "read_only": False}


def _patch_task(conn, task_id: str, body: dict):
    """Apply a partial update to a task, routing status transitions through structured verbs (complete, block, archive)."""
    kb = _kb()
    task = kb.get_task(conn, task_id)
    if not task:
        raise LookupError("task not found")

    status = None
    if "status" in body and body.get("status") not in (None, ""):
        status = _validate_status(str(body.get("status")))
        detail = _sticky_block_detail(conn, task_id)
        if detail and detail.get("sticky") and status not in {"blocked", "archived"}:
            raise RuntimeError(
                "Cannot change card status while an exact terminal action is pending; "
                "approve that exact action first"
            )

    updates = {}
    if "title" in body:
        title = str(body.get("title") or "").strip()
        if not title:
            raise ValueError("title is required")
        updates["title"] = title
    if "body" in body:
        updates["body"] = body.get("body") or None
    if "tenant" in body:
        updates["tenant"] = body.get("tenant") or None
    if "priority" in body:
        try:
            updates["priority"] = int(body.get("priority") or 0)
        except (TypeError, ValueError):
            raise ValueError("priority must be an integer")

    for field, value in updates.items():
        if hasattr(task, field):
            try:
                setattr(task, field, value)
            except Exception:
                pass
    if updates:
        assignments = ", ".join(f"{field} = ?" for field in updates)
        conn.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", [*updates.values(), task_id])
        if hasattr(kb, "_append_event"):
            kb._append_event(conn, task_id, "updated", {"fields": list(updates), "source": "webui"})

    if "assignee" in body:
        if not kb.assign_task(conn, task_id, body.get("assignee") or None):
            raise LookupError("task not found")

    if status is None:
        return
    # Human-Gate v1. The Core guards every exit out of blocked/scheduled on a
    # gated card, but only inside its structured verbs. The direct-write path
    # below (_set_status_direct) is raw SQL and never consults the gate, so a
    # gated card could be moved to todo/triage/ready with no token and no audit
    # trail -- and `todo` is promoted to `ready` by recompute_ready, which hands
    # it straight to the dispatcher. That is a full bypass of the gate (verified
    # 2026-07-15), not a cosmetic gap: it is the exact defence the 2026-07-13
    # incident hardened. The gated 'ready' case reached unblock_task and was
    # refused, but 'todo'/'triage' -- and 'ready' from `scheduled` -- were not.
    #
    # Refuse here, mirroring the agent dashboard (plugin_api.py update_task).
    # The supported release paths stay open and unchanged: the Unblock button
    # (_unblock_gate_aware) and exact-action approval both mint + redeem a grant
    # and leave an audit trail. `done`/`archived` are NOT listed here because
    # their Core verbs already enforce the gate themselves.
    if status in ("ready", "triage", "todo"):
        gate_check = kb.get_task(conn, task_id)
        if (
            gate_check is not None
            and bool(getattr(gate_check, "human_gate", 0))
            and gate_check.status in ("blocked", "scheduled")
        ):
            raise RuntimeError(
                "human-gated card cannot be moved by a direct status change; "
                "use Unblock (or approve its exact terminal action) to release it"
            )
        # Leaving a terminal state is a reopen, and a reopen has obligations the
        # raw path cannot meet: clear completed_at/result (or the card claims to
        # be open and finished at once, and every duration metric lies), demote
        # children that were ready only because this parent was done, and record
        # WHY. _set_status_direct below has no source-status restriction at all,
        # so a drag from Done quietly did none of that. Route it through the verb.
        if gate_check is not None and gate_check.status in ("done", "archived"):
            raise RuntimeError(
                "reopen refused: use the reopen action (it needs a reason and "
                "restores the card's state properly) instead of a direct status change"
            )
    if status == "done":
        # Completing a gated card IS the human deciding -- the same reasoning
        # _unblock_gate_aware already runs on. Without a token the Core refuses
        # and the operator gets the raw gate error, which tells them to wait for
        # an ntfy push or run `hermes kanban gate <id> off` in a shell: a CLI
        # command with no WebUI equivalent. So the one-click path was simply
        # broken, and the advice unusable. Mint + redeem server-side, and record
        # it -- the plaintext token never leaves this function.
        gate_token = _mint_gate_token_for_operator(
            conn, task_id, action="complete", note="Abschluss",
        )
        if not kb.complete_task(
            conn, task_id, result=body.get("result"), summary=body.get("summary"),
            **({"token": gate_token} if gate_token else {}),
        ):
            raise LookupError("task not found")
    elif status == "blocked":
        if not kb.block_task(conn, task_id, reason=body.get("block_reason") or body.get("reason")):
            raise LookupError("task not found")
    elif status == "archived":
        if not kb.archive_task(conn, task_id):
            raise LookupError("task not found")
    elif status == "running":
        # The 'running' state is owned by the kanban dispatcher / claim
        # protocol — entering it via raw UPDATE bypasses claim_lock,
        # claim_expires, started_at, and worker_pid, which leaves the task
        # in a state the dispatcher treats as "phantom claimed" and may
        # reclaim or hide. Match the agent dashboard plugin's contract
        # (plugins/kanban/dashboard/plugin_api.py update_task) by rejecting
        # this transition with HTTP 400. Workers enter 'running' via
        # kb.claim_task(); UI users should use the dispatcher nudge.
        raise ValueError(
            "Cannot set status to 'running' directly; use the dispatcher/claim path"
        )
    elif status == "ready":
        # If the task is currently 'blocked', use the structured unblock
        # verb so the unblocked event fires. Otherwise it's a legitimate
        # drag-drop or click move (e.g. todo → ready, running → ready when
        # the user yanks a stuck worker back to the queue) and we use the
        # claim-aware direct status write.
        current = kb.get_task(conn, task_id)
        if not current:
            raise LookupError("task not found")
        if current.status == "blocked":
            if not kb.unblock_task(conn, task_id):
                raise LookupError("task not found")
        else:
            if not _set_status_direct(conn, task_id, "ready"):
                raise LookupError("task not found")
    elif status in ("triage", "todo"):
        # Direct status write for drag-drop moves between non-running,
        # non-terminal columns. Uses the claim-aware helper that nulls out
        # claim_lock / claim_expires / worker_pid when leaving 'running'
        # and ends any active run with outcome='reclaimed'.
        if not _set_status_direct(conn, task_id, status):
            raise LookupError("task not found")
    else:
        # _validate_status guarantees we never reach here, but be defensive.
        raise ValueError(f"unknown status: {status}")


def _patch_task_payload(task_id: str, body: dict, *, board=None):
    """Validate task_id, open a connection, and delegate field-level updates to _patch_task."""
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id is required")
    kb = _kb()
    with _conn(board=board) as conn:
        _patch_task(conn, task_id, body)
        return {"task": _task_dict(kb.get_task(conn, task_id)), "read_only": False}


def _comment_payload(task_id: str, body: dict, *, board=None):
    """Add a comment to a task and return the new comment_id in a read_only envelope."""
    task_id = str(task_id or "").strip()
    comment_body = str(body.get("body") or "").strip()
    if not task_id:
        raise ValueError("task_id is required")
    if not comment_body:
        raise ValueError("body is required")
    kb = _kb()
    with _conn(board=board) as conn:
        if not kb.get_task(conn, task_id):
            raise LookupError("task not found")
        comment_id = kb.add_comment(conn, task_id, body.get("author") or "webui", comment_body)
        return {"ok": True, "comment_id": comment_id, "read_only": False}


def _link_tasks_payload(body: dict, *, unlink: bool = False, board=None):
    """Create or delete a parent-child dependency link between two tasks."""
    parent_id = str(body.get("parent_id") or "").strip()
    child_id = str(body.get("child_id") or "").strip()
    if not parent_id or not child_id:
        raise ValueError("parent_id and child_id are required")
    kb = _kb()
    with _conn(board=board) as conn:
        if not kb.get_task(conn, parent_id):
            raise LookupError("parent task not found")
        if not kb.get_task(conn, child_id):
            raise LookupError("child task not found")
        if unlink:
            changed = kb.unlink_tasks(conn, parent_id, child_id)
            return {"ok": True, "changed": bool(changed), "parent_id": parent_id, "child_id": child_id, "read_only": False}
        kb.link_tasks(conn, parent_id, child_id)
        return {"ok": True, "parent_id": parent_id, "child_id": child_id, "read_only": False}

def _links_for(conn, task_id: str) -> dict:
    """Return {parents: [...], children: [...]} dependency id lists for a task."""
    kb = _kb()
    return {
        "parents": kb.parent_ids(conn, task_id),
        "children": kb.child_ids(conn, task_id),
    }


def _task_artifacts(conn, task_id: str) -> list:
    """Durable completion artifacts for a task, newest producer run first.

    Rows come from the dispatcher's validated ``task_artifacts`` table; the
    durable copies live under the Hermes home (kanban/artifacts/...), which
    the /api/media allowlist already serves, so the frontend can render them
    as inline media/download links without a new serving route.
    """
    try:
        rows = conn.execute(
            "SELECT durable_path, original_path, content_type, size, producer_run_id "
            "FROM task_artifacts WHERE task_id = ? "
            "ORDER BY producer_run_id DESC, id ASC",
            (task_id,),
        ).fetchall()
    except Exception:
        # Older boards without the table (pre-migration) simply have none.
        return []
    artifacts = []
    for row in rows:
        original = str(row["original_path"] or "")
        artifacts.append({
            "path": str(row["durable_path"] or ""),
            # Display name: the original basename is human-meaningful; the
            # durable basename carries a content hash prefix.
            "name": (original.rsplit("/", 1)[-1] or str(row["durable_path"] or "").rsplit("/", 1)[-1]),
            "content_type": row["content_type"],
            "size": row["size"],
            "run_id": row["producer_run_id"],
        })
    return artifacts


def _task_detail_payload(task_id: str, *, board=None):
    """Return the full task detail: task dict, comments, events, dependency links, and run history."""
    kb = _kb()
    with _conn(board=board) as conn:
        task = kb.get_task(conn, task_id)
        if not task:
            return None
        task_data = _task_dict(task)
        detail = _blocker_details(conn, [task]).get(task_id)
        if detail:
            task_data["block_detail"] = detail
        return {
            "task": task_data,
            "artifacts": _task_artifacts(conn, task_id),
            "comments": [_obj_dict(c) for c in kb.list_comments(conn, task_id)],
            "events": [_obj_dict(e) for e in kb.list_events(conn, task_id)],
            "links": _links_for(conn, task_id),
            "runs": [_obj_dict(r) for r in kb.list_runs(conn, task_id)],
            "read_only": False,
        }


def _events_payload(parsed):
    """Return paginated task events from the board's event log, starting after the ?since= cursor."""
    board = _resolve_board(parsed)
    since = _int_query(parsed, "since", 0, minimum=0)
    limit = _int_query(parsed, "limit", 200, minimum=1, maximum=200)
    with _conn(board=board) as conn:
        rows = conn.execute(
            "SELECT id, task_id, run_id, kind, payload, created_at "
            "FROM task_events WHERE id > ? ORDER BY id ASC LIMIT ?",
            (since, limit),
        ).fetchall()
        events = []
        cursor = since
        for row in rows:
            try:
                payload = json.loads(row["payload"]) if row["payload"] else None
            except Exception:
                payload = None
            events.append({
                "id": row["id"],
                "task_id": row["task_id"],
                "run_id": row["run_id"],
                "kind": row["kind"],
                "payload": payload,
                "created_at": row["created_at"],
            })
            cursor = int(row["id"])
        latest = _latest_event_id(conn)
        if not events:
            cursor = latest if since >= latest else since
        return {"events": events, "cursor": cursor, "latest_event_id": cursor, "read_only": False}


def _config_payload(*, board=None):
    """Return kanban configuration: column names, known assignees, and lane/display settings from hermes_cli.config."""
    kb = _kb()
    try:
        with _conn(board=board) as conn:
            try:
                assignees = list(kb.known_assignees(conn))
            except Exception:
                assignees = []
    except Exception:
        assignees = []
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
    except Exception:
        cfg = {}
    k_cfg = ((cfg.get("dashboard") or {}).get("kanban") or {})
    return {
        "columns": BOARD_COLUMNS,
        "assignees": assignees,
        "default_tenant": k_cfg.get("default_tenant") or "",
        "lane_by_profile": bool(k_cfg.get("lane_by_profile", True)),
        "include_archived_by_default": bool(k_cfg.get("include_archived_by_default", False)),
        "render_markdown": bool(k_cfg.get("render_markdown", True)),
        "read_only": False,
    }


def _update_config_payload(body):
    if not isinstance(body, dict):
        raise ValueError("JSON object body required")
    if "lane_by_profile" not in body:
        raise ValueError("lane_by_profile is required")
    if not isinstance(body.get("lane_by_profile"), bool):
        raise ValueError("lane_by_profile must be boolean")

    from api import config

    config_path = config._get_config_path()
    with config._cfg_lock:
        config_data = config._load_yaml_config_file(config_path)
        dashboard_cfg = config_data.get("dashboard")
        if not isinstance(dashboard_cfg, dict):
            dashboard_cfg = {}
        kanban_cfg = dashboard_cfg.get("kanban")
        if not isinstance(kanban_cfg, dict):
            kanban_cfg = {}
        kanban_cfg["lane_by_profile"] = body["lane_by_profile"]
        dashboard_cfg["kanban"] = kanban_cfg
        config_data["dashboard"] = dashboard_cfg
        config._save_yaml_config_file(config_path, config_data)
    config.reload_config()
    payload = _config_payload()
    payload["lane_by_profile"] = body["lane_by_profile"]
    return payload


def _stats_payload(*, board=None):
    """Return per-status and per-assignee task counts for the board."""
    kb = _kb()
    with _conn(board=board) as conn:
        if hasattr(kb, "board_stats"):
            return kb.board_stats(conn)
        rows = conn.execute(
            "SELECT status, assignee, COUNT(*) AS n FROM tasks WHERE status != 'archived' GROUP BY status, assignee"
        ).fetchall()
        by_status = {}
        by_assignee = {}
        for row in rows:
            n = int(row["n"] or 0)
            by_status[row["status"]] = by_status.get(row["status"], 0) + n
            assignee = row["assignee"] or "unassigned"
            by_assignee[assignee] = by_assignee.get(assignee, 0) + n
        return {"by_status": by_status, "by_assignee": by_assignee}


def _assignees_payload(*, board=None):
    """Return the list of known assignees derived from task history."""
    kb = _kb()
    with _conn(board=board) as conn:
        try:
            assignees = list(kb.known_assignees(conn))
        except Exception:
            rows = conn.execute(
                "SELECT DISTINCT assignee FROM tasks WHERE assignee IS NOT NULL AND assignee != '' ORDER BY assignee"
            ).fetchall()
            assignees = [row["assignee"] for row in rows]
    return {"assignees": assignees}


def _task_log_payload(parsed, task_id: str):
    """Return the raw worker log content and on-disk metadata for a task's dispatcher run."""
    board = _resolve_board(parsed)
    kb = _kb()
    tail = _int_query(parsed, "tail", None, minimum=1, maximum=2_000_000)
    with _conn(board=board) as conn:
        if not kb.get_task(conn, task_id):
            return None
    if not hasattr(kb, "read_worker_log"):
        return {"task_id": task_id, "path": "", "exists": False, "size_bytes": 0, "content": "", "truncated": False}
    content = kb.read_worker_log(task_id, tail_bytes=tail)
    log_path = kb.worker_log_path(task_id) if hasattr(kb, "worker_log_path") else None
    try:
        size = log_path.stat().st_size if log_path and log_path.exists() else 0
    except OSError:
        size = 0
    return {
        "task_id": task_id,
        "path": str(log_path or ""),
        "exists": content is not None,
        "size_bytes": size,
        "content": content or "",
        "truncated": bool(tail and size > tail),
    }


def _bulk_tasks_payload(body: dict, *, board=None):
    """Apply a common mutation (archive/status/assignee/priority) to multiple task ids in a single transaction."""
    ids = [str(i).strip() for i in (body.get("ids") or []) if str(i).strip()]
    if not ids:
        raise ValueError("ids is required")
    results = []
    kb = _kb()
    with _conn(board=board) as conn:
        for task_id in ids:
            entry = {"id": task_id, "ok": True}
            try:
                if not kb.get_task(conn, task_id):
                    entry.update(ok=False, error="not found")
                    results.append(entry)
                    continue
                if body.get("archive"):
                    if not kb.archive_task(conn, task_id):
                        entry.update(ok=False, error="archive refused")
                elif body.get("status") is not None:
                    _patch_task(conn, task_id, {"status": body.get("status")})
                if body.get("assignee") is not None:
                    if not kb.assign_task(conn, task_id, body.get("assignee") or None):
                        entry.update(ok=False, error="assign refused")
                if body.get("priority") is not None:
                    try:
                        priority = int(body.get("priority"))
                    except (TypeError, ValueError):
                        entry.update(ok=False, error="priority must be an integer")
                    else:
                        conn.execute("UPDATE tasks SET priority = ? WHERE id = ?", (priority, task_id))
                        if hasattr(kb, "_append_event"):
                            kb._append_event(conn, task_id, "reprioritized", {"priority": priority, "source": "webui"})
            except Exception as exc:
                entry.update(ok=False, error=str(exc))
            results.append(entry)
    return {"results": results, "read_only": False}


def _dispatch_payload(parsed):
    """Trigger a single-pass kanban dispatcher run and return the dispatch result."""
    board = _resolve_board(parsed)
    kb = _kb()
    dry_run = _bool_query(parsed, "dry_run", False)
    max_spawn = _int_query(parsed, "max", 8, minimum=1, maximum=100)
    if not hasattr(kb, "dispatch_once"):
        raise ValueError("dispatcher is unavailable")
    with _conn(board=board) as conn:
        result = kb.dispatch_once(conn, dry_run=dry_run, max_spawn=max_spawn)
    if isinstance(result, dict):
        return result
    try:
        return asdict(result)
    except TypeError:
        return {"result": str(result)}


def _task_action_payload(task_id: str, body: dict, action: str, *, board=None):
    """Execute a named action (block or unblock) on a task and return the updated task dict."""
    kb = _kb()
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id is required")
    with _conn(board=board) as conn:
        current = kb.get_task(conn, task_id)
        if not current:
            raise LookupError("task not found")
        if action == "block":
            ok = kb.block_task(conn, task_id, reason=body.get("reason") or body.get("block_reason"))
        elif action == "unblock":
            detail = _sticky_block_detail(conn, task_id)
            if detail and detail.get("sticky"):
                raise RuntimeError(
                    "Cannot unblock card while an exact terminal action is pending; "
                    "approve that exact action first"
                )
            note = str(body.get("reason") or "").strip()
            att_id = body.get("attention_id")
            att_ver = body.get("attention_version")
            try:
                att_id = int(att_id) if att_id is not None else None
                att_ver = int(att_ver) if att_ver is not None else None
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "attention_id and attention_version must be integers"
                ) from exc
            ok = _unblock_gate_aware(
                conn, task_id, current, note,
                attention_id=att_id, attention_version=att_ver,
            )
        else:
            raise ValueError(f"invalid action: {action}")
        if not ok:
            raise RuntimeError(f"{action} refused")
        return {"task": _task_dict(kb.get_task(conn, task_id)), "read_only": False}


def _approve_exact_action_payload(task_id: str, body: dict, *, board=None):
    """Atomically approve one current action and unblock only its own card."""
    kb = _kb()
    task_id = str(task_id or "").strip()
    raw_action_id = body.get("pending_action_id")
    if not task_id or raw_action_id in (None, ""):
        raise ValueError("task_id and pending_action_id are required")
    try:
        action_id = int(raw_action_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("pending_action_id must be an integer") from exc
    required = (
        "get_pending_action", "get_pending_action_by_id",
        "approve_pending_action_and_unblock",
    )
    if not all(callable(getattr(kb, name, None)) for name in required):
        raise RuntimeError("exact terminal action approval is unavailable in this Hermes core")
    with _conn(board=board) as conn:
        task = kb.get_task(conn, task_id)
        if task is None:
            raise LookupError("task not found")
        current = kb.get_pending_action(conn, task_id)
        if current is None:
            historical = kb.get_pending_action_by_id(conn, task_id, action_id)
            if historical is None:
                raise LookupError("pending terminal action not found")
            if (
                historical.consumed_at is not None
                or getattr(historical, "cancelled_at", None) is not None
                or historical.expires_at <= int(time.time())
            ):
                raise KanbanGoneError("pending terminal action expired or was already resolved")
            raise RuntimeError("no unresolved exact terminal action exists for this task")
        if int(current.id) != action_id:
            raise RuntimeError("pending terminal action changed; refresh before approving")
        if getattr(current, "approved_at", None) is not None:
            raise RuntimeError("pending terminal action was already approved")
        # Approving an exact action also unblocks the card, so a human_gate=1
        # card needs an OPEN gate for that half — the Core refuses otherwise and
        # the refusal surfaces here as an opaque CAS conflict ("conflicted with
        # newer state"), which reads as a race that never happened. The card is
        # then unreleasable from the WebUI: /unblock refuses while the exact
        # action is pending, and this path refuses on the gate.
        #
        # The authenticated WebUI operator IS the human the gate exists for, so
        # mint + redeem a one-time token in the same step — parity with
        # _unblock_gate_aware and the dashboard's gate_off grant. The plaintext
        # token never leaves this function. Ungated cards mint nothing and pass
        # no token, so their path is byte-for-byte unchanged (this also keeps
        # cores/fakes whose approve seam predates the token parameter working).
        gate_token = None
        if bool(getattr(task, "human_gate", 0)) and callable(getattr(kb, "issue_gate_token", None)):
            gate_token = kb.issue_gate_token(conn, task_id, action="unblock")
            if gate_token:
                try:
                    kb.add_comment(
                        conn, task_id, "webui",
                        "GATE-FREIGABE via WebUI-Cockpit für exakte Terminal-Aktion "
                        f"{action_id} (Token einmalig erzeugt und sofort eingelöst "
                        "durch den eingeloggten Operator)",
                    )
                except Exception:
                    pass
        result = kb.approve_pending_action_and_unblock(
            conn, task_id, action_id, actor="webui",
            **({"token": gate_token} if gate_token else {}),
        )
        if not result:
            latest = kb.get_pending_action_by_id(conn, task_id, action_id)
            if latest and (
                latest.consumed_at is not None
                or getattr(latest, "cancelled_at", None) is not None
                or latest.expires_at <= int(time.time())
            ):
                raise KanbanGoneError("pending terminal action expired or was already resolved")
            # The legacy bool seam collapses every cause into False, so name the
            # one cause an operator can act on. Saying "conflicted with newer
            # state" for a gate refusal describes a race that never happened and
            # sends them retrying a click that can never succeed.
            if bool(getattr(task, "human_gate", 0)) and not gate_token:
                raise RuntimeError(
                    "card is human-gated and no gate token could be issued for it; "
                    "the approval was refused by the gate, not by a state change"
                )
            raise RuntimeError("exact terminal action approval conflicted with newer state")
        return {
            "ok": True,
            "task": _task_dict(kb.get_task(conn, task_id)),
            "read_only": False,
        }


def _unarchive_task_payload(task_id: str, body: dict, *, board=None):
    """Undo an archive, restoring the card to what it was.

    Distinct from reopen, and both are needed. Reopen says "this is not finished
    after all" and therefore clears completed_at/result. Unarchive says "I
    archived this by mistake" and must NOT: the result is evidence, and a card
    archived from `done` belongs back in `done` with its result intact.

    Archiving is one click behind a 4-second confirm and, since the H2 removal,
    needs no grant even for a running card -- but there was no route back on
    this surface at all, only `hermes kanban unarchive` in a shell. For an
    operator without CLI access, archived was a one-way street.
    """
    kb = _kb()
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id is required")
    if not callable(getattr(kb, "unarchive_task", None)):
        raise RuntimeError("unarchiving is unavailable in this Hermes core")
    to_status = body.get("to_status")
    if to_status is not None:
        to_status = str(to_status).strip()
        if to_status not in ("todo", "ready", "done"):
            raise ValueError("to_status must be one of todo|ready|done")
    with _conn(board=board) as conn:
        task = kb.get_task(conn, task_id)
        if task is None:
            raise LookupError("task not found")
        if task.status != "archived":
            raise RuntimeError(
                f"only an archived card can be unarchived (this one is {task.status})"
            )
        if not kb.unarchive_task(conn, task_id, to_status=to_status):
            raise RuntimeError("unarchive refused: the card changed; refresh and retry")
        return {
            "ok": True,
            "task": _task_dict(kb.get_task(conn, task_id)),
            "read_only": False,
        }


def _resume_approved_action_retry_payload(task_id: str, body: dict, *, board=None):
    """Retry an approved exact action that a technical failure parked.

    When a worker exceeds max_runtime_seconds while executing an already
    approved action, the Core kills it and parks the card: the action stays
    `approved`, the projection becomes capability/transient. Nothing resumes it.
    Until 2026-07-15 the UI showed "Approved. Waiting for the resumed worker"
    forever and the only clickable way out was archiving, which cancels the
    human's approval instead of honouring it.

    The Core seam for this existed and was correct all along -- it just had no
    caller anywhere, because it needs origin_run_id and no API ever emitted it.
    """
    kb = _kb()
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id is required")
    if not callable(getattr(kb, "resume_approved_action_retry", None)):
        raise RuntimeError("approved-action retry is unavailable in this Hermes core")
    try:
        attention_id = int(body.get("attention_id"))
        attention_version = int(body.get("attention_version"))
        origin_run_id = int(body.get("origin_run_id"))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "attention_id, attention_version and origin_run_id are required integers"
        ) from exc
    with _conn(board=board) as conn:
        if kb.get_task(conn, task_id) is None:
            raise LookupError("task not found")
        result = kb.resume_approved_action_retry(
            conn, task_id=task_id, expected_attention_id=attention_id,
            expected_attention_version=attention_version,
            expected_origin_run_id=origin_run_id, actor="webui",
        )
        if not result:
            status = getattr(result, "status", None)
            if status == "not_found":
                raise LookupError("parked approved action not found")
            if status == "gone":
                raise KanbanGoneError("the parked approved action is no longer available")
            raise RuntimeError("retry refused: the card changed; refresh and retry")
        return {
            "ok": True,
            "task": _task_dict(kb.get_task(conn, task_id)),
            "read_only": False,
        }


def _reopen_task_payload(task_id: str, body: dict, *, board=None):
    """Bring a done/archived card back to an open column, with a reason.

    Until 2026-07-15 there was no verb for this at all: a rerun auto-completed
    card t_bccbacc4 against its own analyst's advice, and the only way back was a
    raw status write. A later agent hit the identical wall and said so on the
    card: "could not block t_bccbacc4 (unknown id or not in running/ready)".

    Lands in `blocked` by default -- NOT `ready`. On 2026-07-15 the card was
    released to `ready` and the dispatcher re-claimed it within 50 seconds,
    re-running a finished audit before anyone could decide anything.
    """
    kb = _kb()
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id is required")
    reason = str(body.get("reason") or "").strip()
    if not reason:
        raise ValueError("a reason is required to reopen a card")
    to_status = str(body.get("to_status") or "blocked").strip()
    if to_status not in ("blocked", "todo"):
        raise ValueError("to_status must be one of blocked|todo")
    if not callable(getattr(kb, "reopen_task", None)):
        raise RuntimeError("reopening is unavailable in this Hermes core")
    with _conn(board=board) as conn:
        task = kb.get_task(conn, task_id)
        if task is None:
            raise LookupError("task not found")
        if task.status not in ("done", "archived"):
            raise RuntimeError(
                f"only a done or archived card can be reopened (this one is {task.status})"
            )
        if not kb.reopen_task(conn, task_id, actor="webui", reason=reason, to_status=to_status):
            raise RuntimeError("reopen refused: the card changed; refresh and retry")
        return {
            "ok": True,
            "task": _task_dict(kb.get_task(conn, task_id)),
            "read_only": False,
        }


def _reject_exact_action_payload(task_id: str, body: dict, *, board=None):
    """Reject one current exact action without ever executing it.

    The 2026-07-15 incident's real lesson: the cockpit could only ever say YES.
    The analyst that raised the action recommended rejecting it, and there was
    no way to do that -- the operator had to resolve it with a direct DB script.

    Rejection is NOT approval-with-a-different-word, and deliberately not a
    gated act: `resolve_pending_action` settles the action without running the
    command and without unblocking the card, so it needs no gate token (the gate
    guards *exits from blocked*, and this is not one). The card stays blocked on
    purpose -- releasing it is a separate, conscious second click.
    """
    kb = _kb()
    task_id = str(task_id or "").strip()
    raw_action_id = body.get("pending_action_id")
    if not task_id or raw_action_id in (None, ""):
        raise ValueError("task_id and pending_action_id are required")
    try:
        action_id = int(raw_action_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("pending_action_id must be an integer") from exc
    # The reason is optional by operator decision (2026-07-16): the two-click
    # arm already guards against stray clicks, and forcing prose produced
    # filler text, not audit value. An empty reason is still recorded as such
    # so the board shows a deliberate, unexplained rejection — not a gap.
    reason = str(body.get("reason") or "").strip()
    if not callable(getattr(kb, "resolve_pending_action", None)):
        raise RuntimeError("exact terminal action rejection is unavailable in this Hermes core")
    with _conn(board=board) as conn:
        if kb.get_task(conn, task_id) is None:
            raise LookupError("task not found")
        current = kb.get_pending_action(conn, task_id)
        if current is None:
            historical = kb.get_pending_action_by_id(conn, task_id, action_id)
            if historical is None:
                raise LookupError("pending terminal action not found")
            raise KanbanGoneError("pending terminal action expired or was already resolved")
        if int(current.id) != action_id:
            raise RuntimeError("pending terminal action changed; refresh before rejecting")
        result = kb.resolve_pending_action(
            conn, task_id, action_id, expected_version=int(current.version),
        )
        if not result:
            status = getattr(result, "status", None)
            if status == "gone":
                raise KanbanGoneError("pending terminal action expired or was already resolved")
            if status == "not_found":
                raise LookupError("pending terminal action not found")
            raise RuntimeError("rejection refused: the action changed; refresh and retry")
        # Both the board view and the cockpit reach this route, so the comment
        # must not claim a specific surface -- the operator is the same human.
        kb.add_comment(
            conn, task_id, "webui",
            "EXAKTE AKTION ABGELEHNT via WebUI (nicht ausgeführt, Karte bleibt blockiert). "
            + (f"Grund: {reason}" if reason else "Ohne Begründung abgelehnt."),
        )
        return {
            "ok": True,
            "task": _task_dict(kb.get_task(conn, task_id)),
            "read_only": False,
        }


def _mint_gate_token_for_operator(conn, task_id: str, *, action: str, note: str):
    """Issue + hand back a one-time gate grant for an authenticated operator act.

    The authenticated WebUI operator IS the human the gate exists for (the same
    reasoning _unblock_gate_aware and the dashboard's gate_off grant run on), so
    the grant is minted and redeemed server-side and the plaintext never leaves
    the server. Returns None for an ungated card or one not in a gated state --
    callers then pass no token at all, leaving that path byte-identical.

    ``action`` must match the transition the Core will validate: grants are
    action-bound, so an "unblock" token presented to complete_task is refused
    with "token was issued for a different action". That binding is the point --
    one grant authorizes one transition, not any exit the holder fancies.

    Every mint is recorded: a gate release with no trace is how the 2026-07-13
    incident became unreconstructable.
    """
    kb = _kb()
    if not callable(getattr(kb, "issue_gate_token", None)):
        return None
    task = kb.get_task(conn, task_id)
    if task is None or not bool(getattr(task, "human_gate", 0)):
        return None
    if getattr(task, "status", None) not in ("blocked", "scheduled"):
        return None
    token = kb.issue_gate_token(conn, task_id, action=action)
    if not token:
        return None
    try:
        kb.add_comment(
            conn, task_id, "webui",
            f"GATE-FREIGABE via WebUI ({note}): Token einmalig erzeugt und sofort "
            "eingelöst durch den eingeloggten Operator",
        )
    except Exception:
        pass
    return token


def _unblock_gate_aware(
    conn, task_id: str, task, note: str = "", *,
    attention_id=None, attention_version=None,
) -> bool:
    """Unblock a task from the WebUI, transparently clearing a Human-Gate.

    The authenticated WebUI operator IS the human the gate exists for, so a
    human_gate=1 card is released by minting a one-time token and redeeming it
    in the same step (parity with the Telegram gate button; the plaintext token
    never leaves this function). Ungated cards take the plain unblock path.
    Every release is recorded as a comment for the board audit trail.

    Typed non-exact attentions (2026-07-16): the Core's legacy ``unblock_task``
    fail-closes on ANY ``task_attentions`` row ("legacy unblock owns only
    ordinary blocked cards"), so a card the circuit breaker parked with a typed
    projection (decision/gave_up/protocol/...) was unreleasable from the
    cockpit — /unblock returned "refused", surfaced as a bogus "status
    changed, refresh" toast that no refresh could fix. Those cards go through
    ``transition_task_status_with_attention``, the Core's only safe generic
    blocked exit, exactly like the agent dashboard (plugin_api.update_task).
    The CAS pair comes from the cockpit's snapshot when provided (the operator
    releases what they SAW); older cockpit builds fall back to the live
    projection. Exact-action attentions never reach this function —
    ``_task_action_payload`` refuses sticky cards first.
    """
    kb = _kb()
    if not hasattr(kb, "unblock_task"):
        _patch_task(conn, task_id, {"status": "ready"})
        return True
    gated = bool(getattr(task, "human_gate", 0))
    attention = None
    if callable(getattr(kb, "get_current_attention", None)) and callable(
        getattr(kb, "transition_task_status_with_attention", None)
    ):
        try:
            attention = kb.get_current_attention(conn, task_id)
        except Exception:
            attention = None
    if attention is not None and getattr(attention, "action_id", None) is None:
        expected_id = int(attention_id if attention_id is not None else attention.id)
        expected_version = int(
            attention_version if attention_version is not None else attention.version
        )
        token = None
        if gated:
            # transition_* enforces the gate under action="change_status", not
            # "unblock" — a mismatched action is a hard token rejection.
            token = kb.issue_gate_token(conn, task_id, action="change_status")
        result = kb.transition_task_status_with_attention(
            conn, task_id=task_id, status="ready", actor="webui",
            expected_attention_id=expected_id,
            expected_attention_version=expected_version,
            **({"token": token} if token else {}),
        )
        if result:
            try:
                kb.add_comment(
                    conn, task_id, "webui",
                    (
                        "GATE-FREIGABE via WebUI-Cockpit (Token einmalig erzeugt "
                        "und sofort eingelöst durch den eingeloggten Operator)"
                        if gated and token
                        else "UNBLOCK via WebUI-Cockpit"
                    )
                    + f" — typed attention #{expected_id} v{expected_version} aufgelöst"
                    + (f": {note}" if note else ""),
                )
            except Exception:
                pass
            return True
        outcome = getattr(result, "status", "conflict")
        if outcome == "not_found":
            raise LookupError("task not found")
        if outcome == "gone":
            raise RuntimeError(
                "unblock conflict: the blocking attention expired or was "
                "already resolved; refresh"
            )
        if outcome == "gate_refused":
            raise RuntimeError(
                "human-gate refused the release; the gate must be satisfied first"
            )
        raise RuntimeError(
            "unblock conflict: the card or its attention changed; refresh and retry"
        )
    if gated:
        token = kb.issue_gate_token(conn, task_id, action="unblock")
        if not token:
            # Not actually gated/blocked anymore, or token mint refused — fall
            # through to the plain path so a race doesn't hard-fail the click.
            token = None
        try:
            kb.add_comment(
                conn, task_id, "webui",
                "GATE-FREIGABE via WebUI-Cockpit (Token einmalig erzeugt und "
                "sofort eingelöst durch den eingeloggten Operator)"
                + (f": {note}" if note else ""),
            )
        except Exception:
            pass
        if token:
            return bool(kb.unblock_task(conn, task_id, actor="webui", reason=note or None, token=token))
    elif note:
        try:
            kb.add_comment(conn, task_id, "webui", f"UNBLOCK via WebUI-Cockpit: {note}")
        except Exception:
            pass
    return bool(kb.unblock_task(conn, task_id, actor="webui", reason=note or None))


# ---------------------------------------------------------------------------
# Multi-board management
# ---------------------------------------------------------------------------
# These endpoints operate on the on-disk board collection itself rather than
# on the tasks of a single board. They mirror the agent dashboard plugin's
# /boards surface (plugins/kanban/dashboard/plugin_api.py) so that the
# CLI / gateway / dashboard / WebUI all share the same active-board pointer.

def _board_meta_dict(meta):
    """Coerce the library's board metadata dict into a JSON-serialisable
    form. ``list_boards`` returns dicts with Path values for ``directory``;
    json.dumps would refuse those without help."""
    if not isinstance(meta, dict):
        return meta
    out = dict(meta)
    for key in ("directory", "db_path", "path"):
        if key in out and out[key] is not None:
            out[key] = str(out[key])
    return out


def _board_counts_for_slug(slug):
    """Per-status task counts for a board, used to populate the board
    switcher with a live "12 tasks" badge. Mirrors the agent dashboard's
    ``_board_counts`` helper. Returns an empty dict for boards whose
    sqlite file has not been materialized yet (freshly-created boards
    with no tasks)."""
    kb = _kb()
    if not kb.board_exists(slug):
        return {}
    try:
        conn = kb.connect(board=slug)
    except Exception:
        return {}
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks "
            "WHERE status != 'archived' GROUP BY status"
        ).fetchall()
        return {row["status"]: int(row["n"] or 0) for row in rows}
    except Exception:
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _list_boards_payload(parsed):
    """GET /api/kanban/boards — return all boards on disk + active slug.

    Each entry includes per-status counts and an ``is_current`` flag so the
    UI can render the switcher in a single round-trip.
    """
    kb = _kb()
    include_archived = _bool_query(parsed, "include_archived", False)
    boards = kb.list_boards(include_archived=include_archived)
    try:
        current = kb.get_current_board()
    except Exception:
        current = "default"
    visible_slugs = {(_board_meta_dict(meta).get("slug")) for meta in boards}
    default_slug = getattr(kb, "DEFAULT_BOARD", "default")
    if current not in visible_slugs:
        # The on-disk active-board pointer can outlive an archived/deleted board
        # when another CLI/WebUI process removes it. Surface a valid current
        # board instead of letting the frontend pin every subsequent request to
        # a ghost slug and fail with an opaque 404.
        try:
            kb.clear_current_board()
        except Exception:
            pass
        current = default_slug
    out = []
    for raw_meta in boards:
        meta = _board_meta_dict(raw_meta)
        slug = meta.get("slug")
        if slug is None:
            continue
        meta["is_current"] = (slug == current)
        meta["counts"] = _board_counts_for_slug(slug)
        meta["total"] = sum(meta["counts"].values()) if meta["counts"] else 0
        out.append(meta)
    return {"boards": out, "current": current, "read_only": False}


def _create_board_payload(body):
    """POST /api/kanban/boards — create a new board.

    Body fields: ``slug`` (required), ``name``, ``description``, ``icon``,
    ``color``, ``switch`` (bool — set as active after creation, default false).
    Idempotent on slug — repeating returns the existing board metadata.
    """
    kb = _kb()
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    slug = str(body.get("slug") or "").strip()
    if not slug:
        raise ValueError("slug is required")
    try:
        meta = kb.create_board(
            slug,
            name=body.get("name") or None,
            description=body.get("description") or None,
            icon=body.get("icon") or None,
            color=body.get("color") or None,
        )
    except (ValueError, AttributeError) as exc:
        raise ValueError(str(exc)) from exc
    if body.get("switch"):
        try:
            kb.set_current_board(meta["slug"])
        except (ValueError, AttributeError) as exc:
            raise ValueError(str(exc)) from exc
    try:
        current = kb.get_current_board()
    except Exception:
        current = "default"
    return {"board": _board_meta_dict(meta), "current": current, "read_only": False}


def _update_board_payload(slug, body):
    """PATCH /api/kanban/boards/<slug> — update a board's display metadata.

    The slug itself is immutable (changing it would mean moving the on-disk
    directory and re-pointing every saved active-board cookie). Only
    ``name``, ``description``, ``icon``, ``color``, and ``archived`` are
    mutable here; the slug travels in the URL path.
    """
    kb = _kb()
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    try:
        normed = kb._normalize_board_slug(slug)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"invalid board slug: {slug!r}") from exc
    if not normed or not kb.board_exists(normed):
        raise LookupError(f"board {slug!r} does not exist")
    archived = body.get("archived")
    if isinstance(archived, str):
        archived = archived.strip().lower() in {"1", "true", "yes", "on"}
    meta = kb.write_board_metadata(
        normed,
        name=body.get("name"),
        description=body.get("description"),
        icon=body.get("icon"),
        color=body.get("color"),
        archived=archived if isinstance(archived, bool) else None,
    )
    return {"board": _board_meta_dict(meta), "read_only": False}


def _delete_board_payload(slug, parsed):
    """DELETE /api/kanban/boards/<slug> — archive (default) or hard-delete.

    ``?delete=1`` is required to actually remove on-disk artefacts; without
    it the board is just marked archived in its metadata and remains
    enumerable via ``?include_archived=1`` on /boards.
    """
    kb = _kb()
    hard_delete = _bool_query(parsed, "delete", False)
    try:
        normed = kb._normalize_board_slug(slug)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"invalid board slug: {slug!r}") from exc
    if not normed or not kb.board_exists(normed):
        raise LookupError(f"board {slug!r} does not exist")
    # Refuse to delete the default board — that would leave the system
    # without a fallback active board on next CLI / dashboard call.
    try:
        default_slug = getattr(kb, "DEFAULT_BOARD", "default")
    except Exception:
        default_slug = "default"
    if normed == default_slug:
        raise ValueError("cannot remove the default board")
    res = kb.remove_board(normed, archive=not hard_delete)
    try:
        current = kb.get_current_board()
    except Exception:
        current = "default"
    # If we just removed the active board, the library auto-falls-back to
    # default on the next get_current_board() — surface that explicitly so
    # the UI can re-fetch /board on the new active slug.
    return {
        "result": _board_meta_dict(res) if isinstance(res, dict) else res,
        "current": current,
        "read_only": False,
    }


def _switch_board_payload(slug):
    """POST /api/kanban/boards/<slug>/switch — set this board as active.

    The active-board pointer is stored on disk under ``<root>/kanban/current``
    and is shared by the CLI, gateway, dashboard, and WebUI — switching
    here switches everywhere. The UI also keeps a localStorage hint so
    that opening a fresh tab doesn't always have to round-trip to discover
    the active slug, but the on-disk pointer is the source of truth.
    """
    kb = _kb()
    try:
        normed = kb._normalize_board_slug(slug)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"invalid board slug: {slug!r}") from exc
    if not normed or not kb.board_exists(normed):
        raise LookupError(f"board {slug!r} does not exist")
    kb.set_current_board(normed)
    return {"current": normed, "read_only": False}


# ---------------------------------------------------------------------------
# SSE event stream
# ---------------------------------------------------------------------------
# Server-Sent Events let the UI react to task transitions in real time
# without the 30s HTTP polling tax. The agent dashboard uses WebSockets
# for the same purpose; we use SSE because the WebUI's existing transport
# is a synchronous BaseHTTPServer and SSE is the right tool for
# unidirectional server-pushed event streams. The wire-level UX is
# identical from the client's perspective: events arrive within ~300ms
# of being committed to task_events.

# Polling interval matches the agent dashboard's _EVENT_POLL_SECONDS so
# write-to-receive latency is identical between the two surfaces.
_KANBAN_SSE_POLL_SECONDS = 0.3
# Heartbeat keeps proxies/CDNs from reaping the connection on idle boards.
# Identical to the approval/clarify SSE heartbeat.
_KANBAN_SSE_HEARTBEAT_SECONDS = 15.0
# Hard cap on a single SSE batch so a board with thousands of historical
# events doesn't ship them all in one frame. Same as the dashboard.
_KANBAN_SSE_BATCH_LIMIT = 200


def _kanban_sse_fetch_new(board, cursor):
    """Read events with id > cursor from the given board's task_events
    table. Returns ``(new_cursor, events_list)``. Best-effort — returns
    the input cursor and an empty list on any DB error so the SSE loop
    self-heals on transient sqlite contention rather than dropping the
    client."""
    kb = _kb()
    # Guard against a board that's been archived/removed mid-stream:
    # kb.connect(board=<slug>) auto-materialises the directory + DB on
    # first call, which would silently un-archive a board that was just
    # removed. Skip the fetch when the board no longer exists.
    if board is not None:
        try:
            default_slug = getattr(kb, "DEFAULT_BOARD", "default")
        except Exception:
            default_slug = "default"
        if board != default_slug and not kb.board_exists(board):
            return cursor, []
    try:
        conn = kb.connect(board=board)
    except Exception:
        return cursor, []
    try:
        rows = conn.execute(
            "SELECT id, task_id, run_id, kind, payload, created_at "
            "FROM task_events WHERE id > ? ORDER BY id ASC LIMIT ?",
            (int(cursor), _KANBAN_SSE_BATCH_LIMIT),
        ).fetchall()
    except Exception:
        return cursor, []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    out = []
    new_cursor = cursor
    for r in rows:
        payload = None
        try:
            raw = r["payload"]
            if raw:
                payload = json.loads(raw)
        except Exception:
            payload = None
        out.append({
            "id": int(r["id"]),
            "task_id": r["task_id"],
            "run_id": r["run_id"],
            "kind": r["kind"],
            "payload": payload,
            "created_at": int(r["created_at"]) if r["created_at"] is not None else None,
        })
        new_cursor = int(r["id"])
    return new_cursor, out


def _handle_events_sse_stream(handler, parsed):
    """GET /api/kanban/events/stream — long-lived SSE feed of task events.

    Query params:
      since=<int>   Resume from this event id. Defaults to 0 (full backlog
                    on first connect — the client should pass the latest
                    id it knows about so it does not re-receive historical
                    events.) Capped to the most recent _KANBAN_SSE_BATCH_LIMIT.
      board=<slug>  Pin the stream to a specific board. Switching boards
                    requires the client to close and re-open the stream.

    Header (set automatically by EventSource on reconnect):
      Last-Event-ID  Fallback resume cursor when ?since= is absent. The
                     server emits ``id: <event_id>`` on every events frame
                     so the browser can resume cleanly across drops without
                     re-receiving up to _KANBAN_SSE_BATCH_LIMIT events the
                     client already has.

    Mirrors the agent dashboard's WebSocket /events contract event-for-event
    so a client that handles one can handle the other with only the
    transport swapped.
    """
    try:
        board = _resolve_board(parsed)
    except (ValueError, LookupError) as exc:
        return bad(handler, str(exc), status=400 if isinstance(exc, ValueError) else 404)

    qs = parse_qs(parsed.query or "")
    # Resolution chain: ?since= query param → Last-Event-ID header → 0.
    # The Last-Event-ID header is what EventSource sends automatically on
    # reconnect; honouring it lets the browser resume cleanly without the
    # client needing to track the cursor in JS.
    since_raw = (qs.get("since") or [None])[0]
    if since_raw is None:
        try:
            since_raw = handler.headers.get("Last-Event-ID")
        except Exception:
            since_raw = None
    try:
        cursor = int(since_raw) if since_raw is not None else 0
    except (TypeError, ValueError):
        cursor = 0
    if cursor < 0:
        cursor = 0

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    handler.send_header("Connection", "close")
    end_sse_headers(handler)

    # Send an initial frame so the client knows the connection is open
    # and learns the current cursor (in case the server already had a
    # backlog when the client first connected).
    try:
        handler.wfile.write(
            f"event: hello\ndata: {json.dumps({'cursor': cursor, 'board': board})}\n\n".encode("utf-8")
        )
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, ValueError, OSError):
        return True

    last_heartbeat = time.monotonic()
    try:
        while True:
            cursor, events = _kanban_sse_fetch_new(board, cursor)
            if events:
                # Emit `id: <last_event_id>` on every events frame so the
                # browser sets Last-Event-ID on auto-reconnect, letting us
                # resume from there without re-streaming the backlog.
                payload = json.dumps({"events": events, "cursor": cursor})
                frame = (
                    f"id: {cursor}\nevent: events\ndata: {payload}\n\n"
                ).encode("utf-8")
                try:
                    handler.wfile.write(frame)
                    handler.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ValueError, OSError):
                    return True
                last_heartbeat = time.monotonic()
            else:
                # Heartbeat keeps reverse proxies and the browser from
                # closing an idle stream. SSE comments (lines starting
                # with `:`) are ignored by EventSource.
                if (time.monotonic() - last_heartbeat) >= _KANBAN_SSE_HEARTBEAT_SECONDS:
                    try:
                        handler.wfile.write(b": keepalive\n\n")
                        handler.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, ValueError, OSError):
                        return True
                    last_heartbeat = time.monotonic()
            time.sleep(_KANBAN_SSE_POLL_SECONDS)
    except Exception:
        # Any other unexpected exception in the SSE loop should not bubble
        # up to the request handler (which would 500 a long-lived stream).
        return True


def handle_kanban_get(handler, parsed) -> bool | None:
    """Dispatch a Kanban GET. Three-valued return:

    - ``False`` — no Kanban path matched; caller should emit a 404
      (``_kanban_unknown_endpoint``) for genuinely stale-bundle requests.
    - ``None`` — a path matched and the inner handler already sent a
      response via ``bad(...)`` / ``j(...)`` (which both return ``None``).
      The caller MUST NOT emit another response.
    - ``True`` — a path matched and the inner handler succeeded.

    Treat any falsy-but-not-False return (``0``, ``''``, etc.) as a bug and
    audit the new return path; the caller uses ``is False`` identity check
    to distinguish unmatched paths from already-responded paths (#1843).
    """
    path = parsed.path
    try:
        # Multi-board management endpoints — these do NOT take a board arg
        # because they operate on the on-disk board collection itself, not
        # on a single board's tasks.
        if path == "/api/kanban/boards":
            return j(handler, _list_boards_payload(parsed)) or True
        if path == "/api/kanban/board":
            return j(handler, _board_payload(parsed)) or True
        if path == "/api/kanban/config":
            return j(handler, _config_payload(board=_resolve_board(parsed))) or True
        if path == "/api/kanban/stats":
            return j(handler, _stats_payload(board=_resolve_board(parsed))) or True
        if path == "/api/kanban/assignees":
            return j(handler, _assignees_payload(board=_resolve_board(parsed))) or True
        if path == "/api/kanban/events":
            return j(handler, _events_payload(parsed)) or True
        if path == "/api/kanban/events/stream":
            return _handle_events_sse_stream(handler, parsed)
        if path.startswith(_TASK_PREFIX) and path.endswith("/log"):
            task_id = unquote(path[len(_TASK_PREFIX):-len("/log")]).strip("/")
            if not task_id or "/" in task_id:
                return False
            payload = _task_log_payload(parsed, task_id)
            if payload is None:
                return bad(handler, "task not found", status=404)
            return j(handler, payload) or True
        if path.startswith(_TASK_PREFIX):
            task_id = unquote(path[len(_TASK_PREFIX):]).strip("/")
            if not task_id or "/" in task_id:
                return False
            payload = _task_detail_payload(task_id, board=_resolve_board(parsed))
            if payload is None:
                return bad(handler, "task not found", status=404)
            return j(handler, payload) or True
        return False
    except ImportError as exc:
        # hermes_cli not installed (webui-only deploy). Return a clean 503
        # "kanban unavailable" rather than a 500 so the frontend's existing
        # try/catch surfaces a useful toast.
        return bad(handler, f"kanban unavailable: {exc}", status=503)
    except LookupError as exc:
        return bad(handler, str(exc), status=404)
    except ValueError as exc:
        return bad(handler, str(exc))
    except RuntimeError as exc:
        return bad(handler, str(exc), status=409)


def handle_kanban_post(handler, parsed, body) -> bool | None:
    """Dispatch a Kanban POST. See ``handle_kanban_get`` for the
    three-valued ``True | None | False`` contract (#1843)."""
    path = parsed.path
    try:
        # Multi-board management endpoints — `_create_board_payload` and
        # `_switch_board_payload` operate on the on-disk board collection,
        # not on a single board's tasks.
        if path == "/api/kanban/boards":
            return j(handler, _create_board_payload(body)) or True
        # POST /api/kanban/boards/<slug>/switch — set active board
        _BOARDS_PREFIX = "/api/kanban/boards/"
        if path.startswith(_BOARDS_PREFIX) and path.endswith("/switch"):
            slug = unquote(path[len(_BOARDS_PREFIX):-len("/switch")]).strip("/")
            if not slug or "/" in slug:
                return False
            return j(handler, _switch_board_payload(slug)) or True
        # All board-scoped writes accept a ?board=<slug> query param OR a
        # `board` field in the JSON body. Query takes precedence.
        board_q = _resolve_board(parsed)
        board_b = _resolve_board_from_body(body)
        board = board_q if board_q is not None else board_b
        if path == "/api/kanban/dispatch":
            return j(handler, _dispatch_payload(parsed)) or True
        if path == "/api/kanban/tasks/bulk":
            return j(handler, _bulk_tasks_payload(body, board=board)) or True
        if path == "/api/kanban/tasks":
            return j(handler, _create_task_payload(body, board=board)) or True
        if path == "/api/kanban/links":
            return j(handler, _link_tasks_payload(body, board=board)) or True
        if path == "/api/kanban/links/delete":
            return j(handler, _link_tasks_payload(body, unlink=True, board=board)) or True
        if path.startswith(_TASK_PREFIX) and path.endswith("/comments"):
            task_id = path[len(_TASK_PREFIX):-len("/comments")].strip("/")
            return j(handler, _comment_payload(task_id, body, board=board)) or True
        for suffix, action in (("/block", "block"), ("/unblock", "unblock")):
            if path.startswith(_TASK_PREFIX) and path.endswith(suffix):
                task_id = path[len(_TASK_PREFIX):-len(suffix)].strip("/")
                return j(handler, _task_action_payload(task_id, body, action, board=board)) or True
        if path.startswith(_TASK_PREFIX) and path.endswith("/unarchive"):
            task_id = path[len(_TASK_PREFIX):-len("/unarchive")].strip("/")
            return j(handler, _unarchive_task_payload(task_id, body, board=board)) or True
        if path.startswith(_TASK_PREFIX) and path.endswith("/resume-approved-action-retry"):
            task_id = path[len(_TASK_PREFIX):-len("/resume-approved-action-retry")].strip("/")
            return j(handler, _resume_approved_action_retry_payload(task_id, body, board=board)) or True
        if path.startswith(_TASK_PREFIX) and path.endswith("/reopen"):
            task_id = path[len(_TASK_PREFIX):-len("/reopen")].strip("/")
            return j(handler, _reopen_task_payload(task_id, body, board=board)) or True
        if path.startswith(_TASK_PREFIX) and path.endswith("/reject-exact-action"):
            task_id = path[len(_TASK_PREFIX):-len("/reject-exact-action")].strip("/")
            return j(handler, _reject_exact_action_payload(task_id, body, board=board)) or True
        if path.startswith(_TASK_PREFIX) and path.endswith("/approve-exact-action"):
            task_id = path[len(_TASK_PREFIX):-len("/approve-exact-action")].strip("/")
            return j(handler, _approve_exact_action_payload(task_id, body, board=board)) or True
        if path.startswith(_TASK_PREFIX) and path.endswith("/patch"):
            task_id = path[len(_TASK_PREFIX):-len("/patch")].strip("/")
            return j(handler, _patch_task_payload(task_id, body, board=board)) or True
    except ImportError as exc:
        return bad(handler, f"kanban unavailable: {exc}", status=503)
    except LookupError as exc:
        return bad(handler, str(exc), status=404)
    except ValueError as exc:
        return bad(handler, str(exc))
    except KanbanGoneError as exc:
        return bad(handler, str(exc), status=410)
    except RuntimeError as exc:
        return bad(handler, str(exc), status=409)
    return False


def handle_kanban_patch(handler, parsed, body) -> bool | None:
    """Dispatch a Kanban PATCH. See ``handle_kanban_get`` for the
    three-valued ``True | None | False`` contract (#1843)."""
    path = parsed.path
    try:
        if path == "/api/kanban/config":
            return j(handler, _update_config_payload(body)) or True
        # /boards/<slug> routes operate on the on-disk board collection
        # itself — the slug travels in the URL path, not via ?board=. Match
        # them BEFORE resolving the board param so a stray ?board=ghost in
        # the query string doesn't 404 the legitimate `experiments` rename.
        # (Mirrors handle_kanban_post's structure — fixes asymmetry caught
        # by Opus advisor.)
        _BOARDS_PREFIX = "/api/kanban/boards/"
        if path.startswith(_BOARDS_PREFIX):
            slug = unquote(path[len(_BOARDS_PREFIX):]).strip("/")
            if not slug or "/" in slug:
                return False
            return j(handler, _update_board_payload(slug, body)) or True
        # Task-scoped writes accept ?board=<slug> (or body.board) to pin the
        # write to a specific board. Query takes precedence over body.
        board_q = _resolve_board(parsed)
        board_b = _resolve_board_from_body(body)
        board = board_q if board_q is not None else board_b
        if path.startswith(_TASK_PREFIX):
            task_id = unquote(path[len(_TASK_PREFIX):]).strip("/")
            if not task_id or "/" in task_id:
                return False
            return j(handler, _patch_task_payload(task_id, body, board=board)) or True
    except ImportError as exc:
        return bad(handler, f"kanban unavailable: {exc}", status=503)
    except LookupError as exc:
        return bad(handler, str(exc), status=404)
    except ValueError as exc:
        return bad(handler, str(exc))
    except RuntimeError as exc:
        return bad(handler, str(exc), status=409)
    return False


def handle_kanban_delete(handler, parsed, body) -> bool | None:
    """Dispatch a Kanban DELETE. See ``handle_kanban_get`` for the
    three-valued ``True | None | False`` contract (#1843)."""
    path = parsed.path
    try:
        # Same routing reorder as PATCH: /boards/<slug> path-routed first,
        # so a stray ?board=ghost can't 404 a legitimate board archive.
        _BOARDS_PREFIX = "/api/kanban/boards/"
        if path.startswith(_BOARDS_PREFIX):
            slug = unquote(path[len(_BOARDS_PREFIX):]).strip("/")
            if not slug or "/" in slug:
                return False
            return j(handler, _delete_board_payload(slug, parsed)) or True
        board_q = _resolve_board(parsed)
        board_b = _resolve_board_from_body(body)
        board = board_q if board_q is not None else board_b
        if path == "/api/kanban/links":
            return j(handler, _link_tasks_payload(body, unlink=True, board=board)) or True
    except ImportError as exc:
        return bad(handler, f"kanban unavailable: {exc}", status=503)
    except LookupError as exc:
        return bad(handler, str(exc), status=404)
    except ValueError as exc:
        return bad(handler, str(exc))
    except RuntimeError as exc:
        return bad(handler, str(exc), status=409)
    return False
