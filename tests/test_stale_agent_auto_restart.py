"""Stale Agent runtime → self-restart once idle.

The revision guard (test_agent_runtime_revision_guard.py) proves stale
runtimes fail closed. These tests cover the follow-up: instead of blocking
every action until a human restarts the WebUI, the guard arms a one-shot
watcher that exits the process for the service manager to restart — but only
when nothing is streaming and no manual compression job is running.
"""

from __future__ import annotations

import signal
import threading
from unittest.mock import patch

import pytest

from api import agent_runtime


@pytest.fixture(autouse=True)
def _reset_armed_flag():
    agent_runtime._auto_restart_armed.clear()
    yield
    agent_runtime._auto_restart_armed.clear()


# ---------------------------------------------------------------------------
# Env switch
# ---------------------------------------------------------------------------


class TestEnvSwitch:
    def test_default_enabled(self, monkeypatch):
        monkeypatch.delenv(agent_runtime._AUTO_RESTART_ENV, raising=False)
        assert agent_runtime._stale_auto_restart_enabled()

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", " OFF "])
    def test_opt_out_values(self, monkeypatch, value):
        monkeypatch.setenv(agent_runtime._AUTO_RESTART_ENV, value)
        assert not agent_runtime._stale_auto_restart_enabled()

    def test_explicit_enable(self, monkeypatch):
        monkeypatch.setenv(agent_runtime._AUTO_RESTART_ENV, "1")
        assert agent_runtime._stale_auto_restart_enabled()


# ---------------------------------------------------------------------------
# Guard trip arms the watcher exactly once
# ---------------------------------------------------------------------------


def _trip_guard(monkeypatch):
    """Make ensure_agent_runtime_current() observe a changed revision."""
    monkeypatch.setattr(agent_runtime, "_AGENT_REVISION", "old-revision")
    monkeypatch.setattr(
        agent_runtime, "_read_agent_revision", lambda *a, **k: "new-revision"
    )


class TestGuardArmsWatcher:
    def test_stale_revision_arms_one_shot_watcher(self, monkeypatch):
        _trip_guard(monkeypatch)
        monkeypatch.delenv(agent_runtime._AUTO_RESTART_ENV, raising=False)

        started = []

        class _FakeThread:
            def __init__(self, *args, **kwargs):
                started.append(kwargs.get("name"))

            def start(self):
                pass

        with patch.object(agent_runtime.threading, "Thread", _FakeThread):
            with pytest.raises(agent_runtime.AgentRuntimeChangedError):
                agent_runtime.ensure_agent_runtime_current()
            with pytest.raises(agent_runtime.AgentRuntimeChangedError):
                agent_runtime.ensure_agent_runtime_current()

        assert started == ["stale-agent-auto-restart"], (
            "watcher must be armed exactly once across repeated guard trips"
        )

    def test_opt_out_never_arms(self, monkeypatch):
        _trip_guard(monkeypatch)
        monkeypatch.setenv(agent_runtime._AUTO_RESTART_ENV, "0")

        with patch.object(agent_runtime.threading, "Thread") as thread_cls:
            with pytest.raises(agent_runtime.AgentRuntimeChangedError):
                agent_runtime.ensure_agent_runtime_current()
        thread_cls.assert_not_called()
        assert not agent_runtime._auto_restart_armed.is_set()

    def test_current_revision_never_arms(self, monkeypatch):
        monkeypatch.setattr(agent_runtime, "_AGENT_REVISION", "same")
        monkeypatch.setattr(
            agent_runtime, "_read_agent_revision", lambda *a, **k: "same"
        )
        with patch.object(agent_runtime.threading, "Thread") as thread_cls:
            agent_runtime.ensure_agent_runtime_current()
        thread_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Idle detection — fail closed
# ---------------------------------------------------------------------------


class TestIdleDetection:
    def test_active_stream_blocks_restart(self):
        with patch("api.models._active_stream_ids", return_value={"run-1"}):
            assert not agent_runtime._webui_is_idle()

    def test_running_manual_compression_blocks_restart(self):
        from api import routes

        with (
            patch("api.models._active_stream_ids", return_value=set()),
            patch.object(
                routes,
                "_MANUAL_COMPRESSION_JOBS",
                {"sid": {"status": "running"}},
            ),
        ):
            assert not agent_runtime._webui_is_idle()

    def test_finished_compression_job_does_not_block(self):
        from api import routes

        with (
            patch("api.models._active_stream_ids", return_value=set()),
            patch.object(
                routes,
                "_MANUAL_COMPRESSION_JOBS",
                {"sid": {"status": "done"}},
            ),
        ):
            assert agent_runtime._webui_is_idle()

    def test_unreadable_stream_signal_counts_as_busy(self):
        with patch(
            "api.models._active_stream_ids",
            side_effect=RuntimeError("registry unavailable"),
        ):
            assert not agent_runtime._webui_is_idle()

    def test_fully_idle(self):
        from api import routes

        with (
            patch("api.models._active_stream_ids", return_value=set()),
            patch.object(routes, "_MANUAL_COMPRESSION_JOBS", {}),
        ):
            assert agent_runtime._webui_is_idle()


# ---------------------------------------------------------------------------
# Watcher sends SIGTERM only once idle
# ---------------------------------------------------------------------------


class TestWatcherRestart:
    def test_waits_through_busy_then_sigterms(self):
        idle_answers = iter([False, False, True])
        kills = []

        with (
            patch.object(
                agent_runtime, "_webui_is_idle", lambda: next(idle_answers)
            ),
            patch.object(agent_runtime.time, "sleep", lambda _s: None),
            patch.object(
                agent_runtime.os,
                "kill",
                lambda pid, sig: kills.append((pid, sig)),
            ),
        ):
            agent_runtime._auto_restart_when_idle()

        assert kills == [(agent_runtime.os.getpid(), signal.SIGTERM)]

    def test_watcher_thread_is_daemon(self, monkeypatch):
        _trip_guard(monkeypatch)
        monkeypatch.delenv(agent_runtime._AUTO_RESTART_ENV, raising=False)

        captured = []
        real_thread = threading.Thread

        def _capture(*args, **kwargs):
            thread = real_thread(*args, **{**kwargs, "target": lambda: None})
            captured.append(kwargs)
            return thread

        with patch.object(agent_runtime.threading, "Thread", _capture):
            with pytest.raises(agent_runtime.AgentRuntimeChangedError):
                agent_runtime.ensure_agent_runtime_current()

        assert captured and captured[0]["daemon"] is True
