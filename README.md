Review screenshots for nesquena/hermes-webui#6202 (self-hosted voice endpoints).

Captured headlessly against the PR head with `tests/browser_smoke.py`'s server
harness: real `server.py`, isolated state dir, `config.yaml` pre-seeded with the
self-hosted STT/TTS values shown, `HERMES_WEBUI_ALLOW_VOICE_CONFIG_WRITE=1`.
No agent installed, so provider capability probes answer "unavailable" — that
does not affect the surfaces shown here.

Images only; this branch is deliberately not part of the PR.
