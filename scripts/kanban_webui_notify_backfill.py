#!/usr/bin/env python3
"""One-time backfill before enabling the WebUI kanban notify poller.

Until 2026-07-27 ``kanban_create`` wrote ``platform='webui'`` subscription rows
that no consumer ever served (the gateway notifier rejects unknown platforms
before the event claim), so every webui subscription sits at cursor 0 with its
full event history undelivered — 372 events at the time of writing. Enabling
the poller against that state would wake dozens of sessions with weeks-old
results, each wakeup costing a full agent turn.

This script draws the line: past results stay in the kanban board (they are
not lost — artifacts and task results remain queryable), delivery starts fresh
for events created from now on.

Per board DB:
  - ``webui`` subscriptions of tasks that are done/archived/missing are
    deactivated (the gateway unsubscribes after delivering a done task; these
    were never delivered, so nobody ever cleaned them up).
  - remaining ``webui`` subscriptions get their cursor set to the task's
    current MAX(task_events.id): only future events will be delivered.
  - ``__session__`` subscriptions are deactivated entirely: legacy rows from a
    pre-fix WebUI build that stamped the wrong platform string; no consumer
    will ever exist for them.

Run with --dry-run first. Backups: the script refuses to run unless --backup-dir
is given; it copies each DB there via sqlite3's online backup API before
touching it. Rollback = copy the backup over the live DB.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def board_dbs() -> list[Path]:
    dbs = []
    main = HERMES / "kanban.db"
    if main.exists():
        dbs.append(main)
    boards = HERMES / "kanban" / "boards"
    if boards.is_dir():
        for child in sorted(boards.iterdir()):
            db = child / "kanban.db"
            if db.exists():
                dbs.append(db)
    return dbs


def backup(db: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    label = db.parent.name if db.parent.name != ".hermes" else "default"
    dest = backup_dir / f"{label}-kanban.db"
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def migrate(db: Path, *, dry_run: bool) -> dict:
    conn = sqlite3.connect(db, timeout=30)
    conn.row_factory = sqlite3.Row
    stats = {"db": str(db), "deactivated_done": 0, "deactivated_legacy": 0,
             "cursors_advanced": 0}
    try:
        conn.execute("BEGIN IMMEDIATE")
        stats["deactivated_done"] = conn.execute(
            """
            UPDATE kanban_notify_subs SET active = 0
            WHERE platform = 'webui' AND active = 1 AND (
                task_id NOT IN (SELECT id FROM tasks)
                OR task_id IN (SELECT id FROM tasks WHERE status IN ('done','archived'))
            )
            """
        ).rowcount
        stats["deactivated_legacy"] = conn.execute(
            "UPDATE kanban_notify_subs SET active = 0 "
            "WHERE platform = '__session__' AND active = 1"
        ).rowcount
        stats["cursors_advanced"] = conn.execute(
            """
            UPDATE kanban_notify_subs
            SET last_event_id = COALESCE(
                (SELECT MAX(e.id) FROM task_events e
                 WHERE e.task_id = kanban_notify_subs.task_id),
                last_event_id)
            WHERE platform = 'webui' AND active = 1
              AND last_event_id < COALESCE(
                (SELECT MAX(e.id) FROM task_events e
                 WHERE e.task_id = kanban_notify_subs.task_id), 0)
            """
        ).rowcount
        if dry_run:
            conn.execute("ROLLBACK")
        else:
            conn.execute("COMMIT")
    finally:
        conn.close()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--backup-dir", type=Path,
                    help="directory for pre-migration DB backups (required unless --dry-run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, commit nothing")
    args = ap.parse_args()

    if not args.dry_run and not args.backup_dir:
        ap.error("--backup-dir is required for a real run")

    for db in board_dbs():
        if not args.dry_run:
            dest = backup(db, args.backup_dir)
            print(f"backup: {db} -> {dest}")
        stats = migrate(db, dry_run=args.dry_run)
        mode = "DRY-RUN " if args.dry_run else ""
        print(f"{mode}{stats['db']}: deactivated_done={stats['deactivated_done']} "
              f"deactivated_legacy={stats['deactivated_legacy']} "
              f"cursors_advanced={stats['cursors_advanced']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
