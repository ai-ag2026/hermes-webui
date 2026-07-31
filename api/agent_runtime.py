"""Fail-closed guard for in-process Hermes Agent source revisions.

Hermes WebUI currently imports ``run_agent.AIAgent`` into its long-lived server
process. If the Agent checkout changes while that process is alive, Python may
combine already-cached modules with newly-read source. Refuse to reuse that
mixed runtime and require a clean WebUI restart instead.
"""

from __future__ import annotations

from pathlib import Path
import logging
import os
import signal
import sys
import subprocess
import threading
import time

# Retain the discovered path as a diagnostic/test-visible compatibility value;
# runtime identity is deliberately captured from the loaded module below.
from api.config import _AGENT_DIR  # noqa: F401

logger = logging.getLogger(__name__)

_RESTART_MESSAGE = (
    "Hermes Agent was updated while Hermes WebUI was running. "
    "Restart Hermes WebUI before retrying this action."
)
_AUTO_RESTART_NOTE = (
    " Hermes WebUI will restart itself as soon as no session is streaming — "
    "retry afterwards."
)

# ── Stale-runtime auto-restart ──────────────────────────────────────────────
# Once the guard trips, every action stays blocked until a process restart —
# the process knows this, so it schedules one itself instead of waiting for a
# human. The restart only fires when the WebUI is fully idle (no active
# streams/worker runs, no manual compression job), so in-flight turns are
# never interrupted; the service manager (systemd Restart=always) brings the
# process back on the fresh checkout. Disable with
# HERMES_WEBUI_STALE_AGENT_AUTO_RESTART=0 for setups whose supervisor does
# not restart on exit.
_AUTO_RESTART_ENV = "HERMES_WEBUI_STALE_AGENT_AUTO_RESTART"
_AUTO_RESTART_POLL_SECONDS = 15.0
_auto_restart_armed = threading.Event()


def _stale_auto_restart_enabled() -> bool:
    return os.getenv(_AUTO_RESTART_ENV, "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _webui_is_idle() -> bool:
    """True when a restart interrupts nothing: no live runs, no manual jobs.

    Imports are deferred: models/routes import this module at startup, so
    top-level back-imports would be circular. Fail closed — an unreadable
    signal counts as busy, never as idle.
    """
    try:
        from api.models import _active_stream_ids  # noqa: PLC0415

        if _active_stream_ids():
            return False
    except Exception:
        return False
    try:
        from api import routes  # noqa: PLC0415

        with routes._MANUAL_COMPRESSION_JOBS_LOCK:
            for job in routes._MANUAL_COMPRESSION_JOBS.values():
                if job.get("status") == "running":
                    return False
    except Exception:
        return False
    return True


def _auto_restart_when_idle() -> None:
    logger.warning(
        "Agent checkout changed under the running WebUI — restarting "
        "automatically once idle (checked every %.0fs; opt out via %s=0).",
        _AUTO_RESTART_POLL_SECONDS,
        _AUTO_RESTART_ENV,
    )
    while True:
        time.sleep(_AUTO_RESTART_POLL_SECONDS)
        if _webui_is_idle():
            logger.warning(
                "WebUI idle and Agent runtime stale — exiting for a clean "
                "restart by the service manager."
            )
            os.kill(os.getpid(), signal.SIGTERM)
            return


def _arm_stale_runtime_auto_restart() -> None:
    """Schedule a one-shot idle restart; safe to call on every guard trip."""
    if not _stale_auto_restart_enabled():
        return
    if _auto_restart_armed.is_set():
        return
    _auto_restart_armed.set()
    threading.Thread(
        target=_auto_restart_when_idle,
        name="stale-agent-auto-restart",
        daemon=True,
    ).start()


def _read_agent_revision(
    agent_dir: Path | None,
    *,
    module_path: Path | None = None,
) -> str | None:
    """Return the loaded Agent checkout HEAD, or ``None`` if it is not tracked."""
    if agent_dir is None:
        return None

    if module_path is None:
        module = sys.modules.get("run_agent")
        module_file = getattr(module, "__file__", None)
        if not module_file:
            return None
        try:
            module_path = Path(module_file).resolve()
        except (OSError, RuntimeError, TypeError):
            return None

    try:
        worktree_result = subprocess.run(
            ["git", "-C", str(agent_dir), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if worktree_result.returncode != 0:
            return None
        worktree = Path(worktree_result.stdout.strip()).resolve()
        relative_module = module_path.relative_to(worktree).as_posix()
        tracked_result = subprocess.run(
            [
                "git",
                "--literal-pathspecs",
                "-C",
                str(worktree),
                "ls-files",
                "--error-unmatch",
                "--",
                relative_module,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if tracked_result.returncode != 0:
            return None
        revision_result = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--verify", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError):
        return None

    revision = revision_result.stdout.strip()
    return revision if revision_result.returncode == 0 and revision else None


_AGENT_SOURCE_DIR: Path | None = None
_AGENT_MODULE_PATH: Path | None = None
_AGENT_REVISION: str | None = None
_AIAgent = None
_RUNTIME_LOCK = threading.Lock()


class AgentRuntimeChangedError(RuntimeError):
    """Raised when the loaded Agent runtime no longer matches its source tree."""


def _loaded_agent_source_identity() -> tuple[Path, Path] | None:
    """Return the source directory and file that supplied ``run_agent``."""
    module = sys.modules.get("run_agent")
    module_file = getattr(module, "__file__", None)
    if not module_file:
        return None
    try:
        module_path = Path(module_file).resolve()
        return module_path.parent, module_path
    except (OSError, RuntimeError, TypeError):
        return None


def _capture_loaded_agent_revision() -> None:
    """Bind the guard to the checkout that supplied the loaded Agent module."""
    global _AGENT_SOURCE_DIR, _AGENT_MODULE_PATH, _AGENT_REVISION

    if _AGENT_REVISION is not None:
        ensure_agent_runtime_current()
        return

    identity = _loaded_agent_source_identity()
    if identity is None:
        return
    source_dir, module_path = identity
    current_revision = _read_agent_revision(source_dir, module_path=module_path)
    _AGENT_SOURCE_DIR = source_dir
    _AGENT_MODULE_PATH = module_path
    _AGENT_REVISION = current_revision


def ensure_agent_runtime_current() -> None:
    """Reject a known Git checkout change instead of mixing Python modules."""
    if _AGENT_REVISION is None:
        return
    if (
        _read_agent_revision(_AGENT_SOURCE_DIR, module_path=_AGENT_MODULE_PATH)
        != _AGENT_REVISION
    ):
        _arm_stale_runtime_auto_restart()
        message = _RESTART_MESSAGE
        if _auto_restart_armed.is_set():
            message += _AUTO_RESTART_NOTE
        raise AgentRuntimeChangedError(message)


def require_ai_agent_class():
    """Import ``AIAgent`` after proving the loaded source revision is current."""
    ensure_agent_runtime_current()
    from run_agent import AIAgent  # noqa: PLC0415

    _capture_loaded_agent_revision()
    return AIAgent


def get_ai_agent_class():
    """Return ``AIAgent`` while preserving the existing lazy-import retry."""
    global _AIAgent, _AGENT_REVISION

    with _RUNTIME_LOCK:
        ensure_agent_runtime_current()
        if _AIAgent is None:
            try:
                agent_class = require_ai_agent_class()
            except ImportError:
                return None
            _AIAgent = agent_class
        return _AIAgent
