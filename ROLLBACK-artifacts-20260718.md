# Rollback: Artifact-Ausbau (WP1–WP4) 18.07.2026

- Basis-Commit vor Beginn: aa7614f5 (Branch tars/upstream-merge-20260717-2)
- Backup-Branch: backup/pre-artifacts-<stamp> (git branch -l 'backup/pre-artifacts-*')
- Datei-Backup: ~/hermes-webui-backups/pre-artifacts-<stamp>.tgz
- Rückbau komplett: git reset --hard aa7614f5 && sudo systemctl restart hermes-webui
- Rückbau einzelner WPs: Commits sind pro WP getrennt; git revert <commit> reicht.
- Neue Artefakt-Daten liegen in ~/.hermes/webui/artifacts/ — löschbar ohne Seiteneffekt.
- Feature-Flag: artifacts_enabled in ~/.hermes/webui/settings.json — auf false setzen
  deaktiviert WP2/WP4-Routen ohne Rollback.
