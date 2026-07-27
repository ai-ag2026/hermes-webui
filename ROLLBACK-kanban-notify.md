# Rollback: Kanban-Notify-Poller (WebUI-Zustellung)

Eingeführt 2026-07-27. Der Poller (`api/kanban_notify_poller.py`) stellt
Kanban-Terminal-Events an WebUI-Sessions zu (`kanban_notify_subs` mit
`platform='webui'`), indem er server-seitige Agent-Turns startet.

## Sofort abschalten (ohne Deploy)

```bash
# in der systemd-Unit-Umgebung setzen, dann Neustart:
HERMES_WEBUI_KANBAN_NOTIFY=0
sudo systemctl restart hermes-webui
```

Der Poller startet dann nicht; alles andere bleibt funktionsfähig.

## Code zurückbauen

Commit(s) mit `git revert` entfernen; betroffen sind ausschließlich:
- `api/kanban_notify_poller.py` (neu)
- `server.py` (Start-/Stop-Verdrahtung, zwei Blöcke)
- `tests/test_kanban_notify_poller.py` (neu)
- `scripts/kanban_webui_notify_backfill.py` (neu, einmalig gelaufen)
- `scripts/run_tests_hermetic.py`, `tests/hermetic_policy.py` (Testinfra-Übernahme)

## Datenbank-Migration zurücknehmen

Die Backfill-Migration vom 2026-07-27 hat in allen Board-DBs
`platform='webui'`-Subs abgeschlossener Tasks sowie alle `__session__`-Subs
auf `active=0` gesetzt (Zahlen: default 131+20, amalun-mvp-closeout 22,
drift-control 2). Vorher-Backups (sqlite online backup):

```
~/.hermes/backups/kanban-webui-notify-20260727/<board>-kanban.db
```

Rollback = WebUI + Gateway stoppen, Backup über die Live-DB kopieren,
Dienste starten. Achtung: setzt auch alle seitdem aufgelaufenen
Task-/Event-Änderungen zurück — im Zweifel stattdessen gezielt
`UPDATE kanban_notify_subs SET active=1 WHERE …`.
