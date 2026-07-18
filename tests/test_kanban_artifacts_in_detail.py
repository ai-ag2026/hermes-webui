"""WP6: kanban task detail exposes durable completion artifacts as media.

The dispatcher records validated deliverables in ``task_artifacts``; the
bridge's task-detail payload must surface them so the WebUI can render real
media (inline images / download links via /api/media) instead of a path
string buried in the result text.
"""

from __future__ import annotations

import importlib
import re
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


@pytest.fixture
def kb_real(tmp_path, monkeypatch):
    for mod in ("hermes_cli", "hermes_cli.kanban_db"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb

    kb.init_db()
    assert str(kb.kanban_db_path()).startswith(str(tmp_path)), "must not touch the live board"
    return kb


@pytest.fixture
def bridge(kb_real, monkeypatch):
    import api.kanban_bridge as _bridge

    return importlib.reload(_bridge)


def _insert_artifact(conn, task_id, *, run_id=1, original="/tmp/chart.png",
                     durable="/x/kanban/artifacts/t/1/abc-chart.png",
                     content_type="image/png", size=1234):
    conn.execute(
        "INSERT INTO task_artifacts (task_id, producer_run_id, original_path, "
        "durable_path, sha256, size, content_type, validated_at, retention_class) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (task_id, run_id, original, durable, "0" * 64 + durable, size,
         content_type, int(time.time()), "default"),
    )


class TestBridgeArtifacts:
    def test_detail_payload_contains_artifacts(self, bridge, kb_real):
        with bridge._conn() as conn:
            task_id = kb_real.create_task(conn, title="report task", assignee="worker")
            _insert_artifact(conn, task_id)
            _insert_artifact(
                conn, task_id, run_id=2, original="/tmp/report.pdf",
                durable="/x/kanban/artifacts/t/2/def-report.pdf",
                content_type="application/pdf",
            )
            conn.commit()
        payload = bridge._task_detail_payload(task_id)
        arts = payload["artifacts"]
        assert len(arts) == 2
        # newest producer run first
        assert arts[0]["name"] == "report.pdf"
        assert arts[0]["content_type"] == "application/pdf"
        assert arts[1]["name"] == "chart.png"
        assert arts[1]["path"].endswith("abc-chart.png")
        assert arts[1]["size"] == 1234

        # Task without artifacts → empty list (same board, avoids a second
        # fixture setup whose module re-discovery is home-path fragile).
        with bridge._conn() as conn:
            plain_id = kb_real.create_task(conn, title="plain task", assignee="worker")
            conn.commit()
        assert bridge._task_detail_payload(plain_id)["artifacts"] == []


class TestFrontendWiring:
    def test_artifact_renderer_exists_and_used(self):
        assert "function _kanbanArtifactHtml" in PANELS_JS
        assert "kanban-detail-artifacts" in PANELS_JS
        assert "artifacts.map(_kanbanArtifactHtml)" in PANELS_JS

    def test_images_render_inline_others_download(self):
        body = PANELS_JS[PANELS_JS.find("function _kanbanArtifactHtml"):][:1600]
        assert "api/media?path=" in body
        assert "msg-media-img" in body, "images must render inline"
        assert "msg-media-link" in body, "non-images must be download links"
        assert "target=\"_blank\"" in body

    def test_i18n_keys_present(self):
        for key in ("kanban_artifacts_count", "kanban_no_artifacts"):
            occurrences = len(re.findall(rf"^\s*{key}:", I18N_JS, re.M))
            assert occurrences >= 2, f"{key} missing from locales"
