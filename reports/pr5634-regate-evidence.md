# PR #5634 rebase and visible-streaming re-gate evidence

Generated: 2026-07-10T06:33:27+02:00

## Git state

- Local rebased head: `cc6dbe159563e4f48f6d45eb846187c29f9a2c47`
- Rebase base: `de0e6eb452f13beea6b343d759f1412f17c65498` (`origin/master`)
- Ancestry at verification: `0 behind / 12 ahead`
- Original remote PR/fork head before update: `17f27165e1281aa815229df0170e5768deb61856`
- Working tree: clean

## Automated verification

Canonical focused/adjacent suite:

```text
./scripts/test.sh \
  tests/test_issue5121_provider_auth_terminal_error.py \
  tests/test_stale_stream_writeback.py \
  tests/test_session_sidecar_repair.py \
  tests/test_recovered_journal_context.py \
  tests/test_core_data_loss_cases.py \
  tests/test_pr5634_historical_tool_reasoning_fragments.py \
  tests/test_1694_terminal_cleanup_ownership.py \
  tests/test_live_activity_timeline.py \
  -q

167 passed in 5.30s
```

Static verification:

- `python3 scripts/ruff_lint.py --diff origin/master` -> 0 findings on added/modified lines
- `git diff --check origin/master...HEAD` -> clean

## Browser/DOM verification

An isolated WebUI was served from this worktree on loopback port 8794 with isolated `HERMES_HOME` and `HERMES_WEBUI_STATE_DIR`. A deterministic session contained:

1. cancelled historical turn with partial text `Same partial progress` and a completed terminal tool call;
2. a separate cancelled historical turn with the identical partial text and a completed read-file tool call;
3. a successful current turn with one final assistant answer.

The two historical Worklog disclosures were expanded in Chromium. Runtime DOM readback returned:

```json
{
  "visiblePartialRows": 2,
  "finalRows": 1,
  "noResponseRows": 0
}
```

This proves the cross-turn duplicate text is retained in two separately visible historical rows, while the current successful turn has exactly one final reply and no synthetic `No response from provider` artifact.

Visual evidence:

- `/home/manfred/.hermes/workspace/reports/pr5634-regate/pr5634-visible-cross-turn-proof.png`
- PNG: 780x1581, 156129 bytes
- SHA-256: `7d7ba2cb25a08c31abdbaab2f0c835ebed0a9492d0c7920028a902e132d88a3f`

The contact sheet contains three unmodified WebUI viewport captures (historical turn 1, historical turn 2, and the successful final turn) with only section labels added between captures.
