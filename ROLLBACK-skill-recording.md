# Rückbau: „Record a Skill" (Bildschirmaufnahme → SKILL.md)

**Feature-Branch:** `tars/skill-recording-20260724` · **Basis:** `90e503bd` auf `tars/upstream-merge-20260723`
**Stand:** 24.07.2026 · **Live-Deploy: NICHT erfolgt** (eigenes Go erforderlich)

## Backups vor der Änderung

| Was | Pfad |
|---|---|
| Code (ohne .git/.worktrees/.venv/node_modules) | `~/.hermes/backups/webui/hermes-webui-code-pre-skill-record-20260724T233616.tar.gz` (22 MB) |
| Alle git-Refs (verifiziert, komplette Historie) | `~/.hermes/backups/webui/hermes-webui-refs-pre-skill-record-20260724T233616.bundle` (55 MB) |

## Stufe 1 — Feature abschalten (kein Code-Rückbau nötig)

Das Feature ist **standardmäßig aus**. Es existiert nur, wenn in der `config.yaml` des aktiven Profils
gesetzt ist:

```yaml
webui:
  skill_recording:
    enabled: true
```

**Rückbau:** `enabled: false` setzen (oder den Block löschen) und die WebUI neu starten.
Danach gilt:
* der Aufnahme-Button erscheint nicht mehr im Skills-Tab,
* alle neuen Endpunkte (`/api/skills/record*`) antworten mit `404`, als gäbe es sie nicht,
* keine Job-Verarbeitung, kein ffmpeg-Aufruf, kein Scratch-Verzeichnis.

Kein bestehender Pfad ändert sein Verhalten, wenn das Flag aus ist.

## Stufe 2 — Code entfernen

```bash
cd /home/manfred/hermes-webui
git checkout tars/upstream-merge-20260723
git branch -D tars/skill-recording-20260724     # erst wenn wirklich verworfen
```

Wiederherstellung aus dem Bundle, falls der Branch schon weg ist:

```bash
git fetch ~/.hermes/backups/webui/hermes-webui-refs-pre-skill-record-20260724T233616.bundle \
  'refs/heads/*:refs/heads/restored/*'
```

## Was das Feature NICHT anfasst

* keine Änderung an `MAX_UPLOAD_BYTES` oder am bestehenden `parse_multipart` — der Aufnahme-Upload
  hat einen eigenen Streaming-Pfad,
* kein `_handle_skill_save`, kein bestehender Skill-Endpunkt,
* **kein git** im Skill-Katalog (Entscheid Manfred 24.07., Option B): die Versionierung bleibt
  ausschließlich beim `tars_ssot_snapshot_cron.sh`,
* keine Infrastruktur: kein NGINX, kein Bind, kein Zertifikat, kein Dienst außer der WebUI selbst.

## Nebenwirkungen, die ein Rückbau NICHT rückgängig macht

* **Gespeicherte Skills bleiben.** Ein über das Feature erzeugter Skill liegt danach ganz normal im
  Root-Katalog (`~/.hermes/skills/<kategorie>/<name>/SKILL.md`) und ist am Frontmatter erkennbar:
  `metadata.hermes.source: screen-recording`. Entfernen wie jeder andere Skill.
* **Backups aus dem Commit-Service bleiben.** Jeder Speichervorgang legt über
  `system/backup-skills.sh pre-skill-record` einen Tarball an (~46 MB, Rotation 30).
  Aufräumen: `ls -t ~/.hermes/backups/skills/skills-*-pre-skill-record*.tar.gz`.
* Rohmedien (Video/Audio) bleiben **nicht** liegen — sie werden in jedem Ausgang gelöscht,
  Scratch ist `/tmp/hermes-skill-rec` und überlebt ohnehin keinen Neustart.
