"""Channels panel — messaging platform config, pairing approvals, webhooks.

Exposes ``/api/channels*`` for the WebUI's "Channels" nav panel: a generic,
upstream-PR-fit surface for configuring messaging-platform credentials,
approving/revoking DM pairing requests, and managing webhook subscriptions.

Write path (platform enable/env, pairing approve/revoke, webhook CRUD) mirrors
``api/agent_config_bridge.py``'s two-tier design:

- No agent checkout discovered (standalone WebUI, CI): platform enable/env
  writes fall back to the WebUI's own writers (``api.config``'s comment-
  preserving-ish YAML round trip and ``api.providers._write_env_file``).
  Pairing and webhooks have NO standalone equivalent — they wrap live gateway
  state (``gateway.pairing.PairingStore`` / ``hermes_cli.webhook``) that only
  exists once an agent checkout is present, so those endpoints report
  ``agent_available: false`` and empty data instead of fabricating state.
- Agent checkout discovered: writes route through ``hermes_cli.config``
  (comment-preserving, .env secrets) using the agent's context-local
  Hermes-home override, scoped to the WebUI's active profile.

All writes are gated behind ``HERMES_WEBUI_ALLOW_CHANNELS_WRITE`` (fail-closed
403) since messaging credentials and webhook secrets are high-value targets.
Reads are always open so operators can see current state without the flag.

Config/credential changes here only take effect after a gateway restart —
this module never restarts the gateway itself; responses carry
``restart_required: true`` so the UI can point at the existing restart control.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from api.config import (
    _AGENT_DIR,
    _cfg_lock,
    _get_config_path,
    _load_yaml_config_file,
    _save_yaml_config_file,
    reload_config,
)
from api.helpers import bad, j
from api.profiles import get_active_hermes_home
from api.providers import _load_env_file, _write_env_file

logger = logging.getLogger(__name__)

WRITE_GATE_ENV = "HERMES_WEBUI_ALLOW_CHANNELS_WRITE"
_MASKED_PLACEHOLDER = "••••••"
_WEBHOOK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


# ── Write gate ─────────────────────────────────────────────────────────────

def _writes_allowed() -> bool:
    return os.getenv(WRITE_GATE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _require_write_gate(handler) -> bool:
    """Send a fail-closed 403 and return False when writes are gated off."""
    if _writes_allowed():
        return True
    bad(
        handler,
        f"Channel writes are disabled. Set {WRITE_GATE_ENV}=1 to enable.",
        status=403,
    )
    return False


# ── Agent bridge probe (mirrors api/agent_config_bridge.py) ────────────────

_import_lock = threading.Lock()
_import_state: Optional[str] = None
_REQUIRED_AGENT_CALLABLES = (
    "save_config",
    "load_config",
    "save_env_value",
    "write_platform_config_field",
)


def _probe_agent() -> str:
    global _import_state
    if _import_state is not None:
        return _import_state
    with _import_lock:
        if _import_state is not None:
            return _import_state
        if _AGENT_DIR is None:
            _import_state = "unavailable:no agent checkout discovered"
            return _import_state
        try:
            import hermes_constants  # noqa: F401
            from hermes_cli import config as _agent_config

            for required in _REQUIRED_AGENT_CALLABLES:
                if not callable(getattr(_agent_config, required, None)):
                    raise ImportError(f"hermes_cli.config.{required} missing")
            _import_state = "ok"
        except BaseException as exc:  # ImportError, SyntaxError, etc.
            logger.warning("channels agent bridge unavailable: %s", exc)
            _import_state = f"unavailable:{exc}"
    return _import_state


def agent_available() -> bool:
    """True when platform/pairing/webhook writes can route through the agent."""
    return _probe_agent() == "ok"


@contextmanager
def _scoped_agent_home(home: Path):
    """Scope agent-side path resolution to *home* for the current context."""
    import hermes_constants

    token = hermes_constants.set_hermes_home_override(str(home))
    try:
        yield
    finally:
        hermes_constants.reset_hermes_home_override(token)


# ── MVP platform catalog ────────────────────────────────────────────────────
# Hand-picked subset of the agent's messaging platforms for this first cut.
# Mirrors the real env-var names/required-ness the agent's own catalog uses
# (hermes_cli/web_server.py _PLATFORM_OVERRIDES) so credentials saved here are
# byte-for-byte what the gateway reads — no WebUI-invented env vars.
_MVP_CATALOG: tuple[Dict[str, Any], ...] = (
    {
        "id": "telegram",
        "name": "Telegram",
        "docs_url": "https://core.telegram.org/bots/features#botfather",
        "hint": "Create a bot with @BotFather and paste the token.",
        "qr_only": False,
        "env_schema": (
            {"key": "TELEGRAM_BOT_TOKEN", "label": "Bot token", "required": True, "secret": True},
            {"key": "TELEGRAM_ALLOWED_USERS", "label": "Allowed user IDs (comma-separated)", "required": False, "secret": False},
        ),
    },
    {
        "id": "discord",
        "name": "Discord",
        "docs_url": "https://discord.com/developers/applications",
        "hint": "Create a bot at the Discord Developer Portal and paste its token.",
        "qr_only": False,
        "env_schema": (
            {"key": "DISCORD_BOT_TOKEN", "label": "Bot token", "required": True, "secret": True},
            {"key": "DISCORD_ALLOWED_USERS", "label": "Allowed user IDs (comma-separated)", "required": False, "secret": False},
        ),
    },
    {
        "id": "slack",
        "name": "Slack",
        "docs_url": "https://api.slack.com/apps",
        "hint": "Create a Slack app with Socket Mode enabled, then paste the bot and app tokens.",
        "qr_only": False,
        "env_schema": (
            {"key": "SLACK_BOT_TOKEN", "label": "Bot token", "required": True, "secret": True},
            {"key": "SLACK_APP_TOKEN", "label": "App token", "required": True, "secret": True},
            {"key": "SLACK_ALLOWED_USERS", "label": "Allowed member IDs (comma-separated)", "required": False, "secret": False},
        ),
    },
    {
        "id": "whatsapp",
        "name": "WhatsApp",
        "docs_url": "https://github.com/tulir/whatsmeow",
        "hint": "QR pairing is not yet available in the WebUI — run `hermes whatsapp login` in a terminal, then enable it here.",
        "qr_only": True,
        "env_schema": (
            {"key": "WHATSAPP_ALLOWED_USERS", "label": "Allowed users (comma-separated)", "required": False, "secret": False},
        ),
    },
)
_MVP_CATALOG_IDS = frozenset(entry["id"] for entry in _MVP_CATALOG)


def _looks_secret(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASS"))


def _plugin_catalog_entries() -> List[Dict[str, Any]]:
    """Generic passthrough for agent-registered plugin platforms beyond the MVP set.

    Best-effort: any failure (no agent, older agent without the registry, etc.)
    just means the catalog stays at the MVP four — never an error.
    """
    if not agent_available():
        return []
    try:
        with _scoped_agent_home(get_active_hermes_home()):
            from gateway.platform_registry import platform_registry

            entries: List[Dict[str, Any]] = []
            for entry in platform_registry.plugin_entries():
                if entry.name in _MVP_CATALOG_IDS:
                    continue
                required = tuple(entry.required_env or ())
                env_schema = tuple(
                    {
                        "key": key,
                        "label": key.replace("_", " ").title(),
                        "required": True,
                        "secret": _looks_secret(key),
                    }
                    for key in required
                )
                entries.append(
                    {
                        "id": entry.name,
                        "name": entry.label or entry.name.replace("_", " ").title(),
                        "docs_url": "",
                        "hint": entry.install_hint or "",
                        "qr_only": False,
                        "env_schema": env_schema,
                    }
                )
            return entries
    except Exception:
        logger.debug("channels: plugin catalog unavailable", exc_info=True)
        return []


def _platform_catalog() -> List[Dict[str, Any]]:
    return list(_MVP_CATALOG) + _plugin_catalog_entries()


def _catalog_lookup(platform_id: str) -> Optional[Dict[str, Any]]:
    for entry in _platform_catalog():
        if entry["id"] == platform_id:
            return entry
    return None


# ── Config/env readers (never fabricate values in a GET response) ──────────

def _read_config_and_env(home: Path) -> tuple[dict, dict]:
    if agent_available():
        try:
            with _scoped_agent_home(home):
                from hermes_cli.config import load_config

                return load_config(), _load_env_file(home / ".env")
        except Exception:
            logger.debug("channels: agent load_config failed, using WebUI reader", exc_info=True)
    return _load_yaml_config_file(_get_config_path()), _load_env_file(home / ".env")


def _platform_payload(entry: Dict[str, Any], config: dict, env: dict) -> Dict[str, Any]:
    platforms_cfg = config.get("platforms") if isinstance(config.get("platforms"), dict) else {}
    plat_cfg = platforms_cfg.get(entry["id"]) if isinstance(platforms_cfg, dict) else None
    enabled = bool(plat_cfg.get("enabled")) if isinstance(plat_cfg, dict) else False

    env_vars = []
    required_keys = []
    for field in entry["env_schema"]:
        key = field["key"]
        is_set = bool(env.get(key))
        if field["required"]:
            required_keys.append(key)
        env_vars.append(
            {
                "key": key,
                "label": field["label"],
                "required": field["required"],
                "secret": field["secret"],
                # Never the real value — only whether it's set, and a fixed
                # mask placeholder for secret fields so the UI can render a
                # "configured" state without ever seeing the credential.
                "is_set": is_set,
                "masked_value": _MASKED_PLACEHOLDER if (is_set and field["secret"]) else None,
            }
        )

    configured = all(env.get(k) for k in required_keys)
    return {
        "id": entry["id"],
        "name": entry["name"],
        "docs_url": entry.get("docs_url", ""),
        "hint": entry.get("hint", ""),
        "qr_only": bool(entry.get("qr_only")),
        "enabled": enabled,
        "configured": configured,
        "env_schema": env_vars,
    }


# ── Platform writes ──────────────────────────────────────────────────────

def _clean_env_updates(entry: Dict[str, Any], submitted: dict) -> Dict[str, str]:
    allowed = {field["key"] for field in entry["env_schema"]}
    updates: Dict[str, str] = {}
    for key, value in (submitted or {}).items():
        if key not in allowed:
            raise ValueError(f"{key} is not configurable for {entry['name']}")
        if value == _MASKED_PLACEHOLDER:
            continue  # unchanged placeholder — caller didn't submit a new secret
        trimmed = str(value).strip() if value is not None else ""
        if trimmed:
            updates[key] = trimmed
    return updates


def _write_platform(home: Path, entry: Dict[str, Any], enabled: Optional[bool], env_updates: Dict[str, str]) -> None:
    if agent_available():
        with _scoped_agent_home(home):
            from hermes_cli.config import save_env_value, write_platform_config_field

            for key, value in env_updates.items():
                save_env_value(key, value)
            if enabled is not None:
                write_platform_config_field(entry["id"], "enabled", bool(enabled))
        return

    # Standalone fallback: WebUI's own .env and config.yaml writers. Uses
    # _get_config_path() (not home/"config.yaml" directly) so a deployment
    # with HERMES_CONFIG_PATH set writes/reloads the same file WebUI itself
    # reads for every other config-editing route (MCP servers, settings, …).
    if env_updates:
        _write_env_file(home / ".env", env_updates)
    if enabled is not None:
        config_path = _get_config_path()
        with _cfg_lock:
            config = _load_yaml_config_file(config_path)
            platforms_cfg = config.get("platforms")
            if not isinstance(platforms_cfg, dict):
                platforms_cfg = {}
            plat_cfg = platforms_cfg.get(entry["id"])
            if not isinstance(plat_cfg, dict):
                plat_cfg = {}
            plat_cfg["enabled"] = bool(enabled)
            platforms_cfg[entry["id"]] = plat_cfg
            config["platforms"] = platforms_cfg
            _save_yaml_config_file(config_path, config)
        reload_config()


# ── Pairing store access ────────────────────────────────────────────────────

def _pairing_store():
    from gateway.pairing import PairingStore

    return PairingStore()


# ── Webhook helpers ──────────────────────────────────────────────────────

def _webhook_summary(name: str, route: dict, base_url: str) -> Dict[str, Any]:
    deliver_extra = route.get("deliver_extra")
    deliver_chat_id = deliver_extra.get("chat_id") if isinstance(deliver_extra, dict) else None
    return {
        "name": name,
        "description": route.get("description", ""),
        "events": list(route.get("events") or []),
        "deliver": route.get("deliver", "log"),
        "deliver_chat_id": deliver_chat_id,
        "created_at": route.get("created_at"),
        "url": f"{base_url}/webhooks/{name}",
        # Secret is masked on read; the real value is only ever returned once,
        # in the create response.
        "secret_set": bool(route.get("secret")),
        "enabled": route.get("enabled", True) is not False,
    }


# ── GET handlers ────────────────────────────────────────────────────────────

def _handle_channels_get(handler) -> bool:
    home = get_active_hermes_home()
    config, env = _read_config_and_env(home)
    platforms = [_platform_payload(entry, config, env) for entry in _platform_catalog()]
    j(
        handler,
        {
            "platforms": platforms,
            "writable": _writes_allowed(),
            "write_gate_env": WRITE_GATE_ENV,
            "agent_available": agent_available(),
        },
    )
    return True


def _handle_pairing_get(handler) -> bool:
    if not agent_available():
        j(
            handler,
            {
                "pending": [],
                "approved": [],
                "agent_available": False,
                "writable": _writes_allowed(),
            },
        )
        return True
    try:
        with _scoped_agent_home(get_active_hermes_home()):
            store = _pairing_store()
            pending = store.list_pending()
            approved = store.list_approved()
    except Exception:
        logger.exception("channels: failed to load pairing data")
        return bad(handler, "Failed to load pairing data", status=500) or True
    j(
        handler,
        {
            "pending": pending,
            "approved": approved,
            "agent_available": True,
            "writable": _writes_allowed(),
        },
    )
    return True


def _handle_webhooks_get(handler) -> bool:
    if not agent_available():
        j(
            handler,
            {
                "enabled": False,
                "base_url": "",
                "subscriptions": [],
                "agent_available": False,
                "writable": _writes_allowed(),
            },
        )
        return True
    try:
        with _scoped_agent_home(get_active_hermes_home()):
            import hermes_cli.webhook as wh

            enabled = wh._is_webhook_enabled()
            base_url = wh._get_webhook_base_url()
            subs = wh._load_subscriptions()
            summary = [_webhook_summary(name, route, base_url) for name, route in subs.items()]
    except Exception:
        logger.exception("channels: failed to load webhooks")
        return bad(handler, "Failed to load webhooks", status=500) or True
    j(
        handler,
        {
            "enabled": enabled,
            "base_url": base_url,
            "subscriptions": summary,
            "agent_available": True,
            "writable": _writes_allowed(),
        },
    )
    return True


def handle_channels_get(handler, parsed) -> bool:
    path = parsed.path
    if path == "/api/channels":
        return _handle_channels_get(handler)
    if path == "/api/channels/pairing":
        return _handle_pairing_get(handler)
    if path == "/api/channels/webhooks":
        return _handle_webhooks_get(handler)
    return False


# ── POST handlers ────────────────────────────────────────────────────────

def _handle_channel_platform_post(handler, platform_id: str, body: dict) -> bool:
    entry = _catalog_lookup(platform_id)
    if not entry:
        return bad(handler, f"Unknown channel platform: {platform_id}", status=404) or True
    if not _require_write_gate(handler):
        return True
    try:
        env_updates = _clean_env_updates(entry, body.get("env") or {})
    except ValueError as exc:
        return bad(handler, str(exc), status=400) or True

    enabled = body.get("enabled")
    if enabled is not None:
        enabled = bool(enabled)

    home = get_active_hermes_home()
    try:
        _write_platform(home, entry, enabled, env_updates)
    except Exception:
        logger.exception("channels: failed to update platform %s", platform_id)
        return bad(handler, "Failed to save channel configuration", status=500) or True

    logger.info(
        "Channel platform updated: platform=%s enabled=%s env_keys=%s",
        platform_id,
        enabled,
        sorted(env_updates),
    )
    j(handler, {"ok": True, "platform": platform_id, "restart_required": True})
    return True


def _handle_pairing_approve(handler, body: dict) -> bool:
    if not _require_write_gate(handler):
        return True
    if not agent_available():
        return bad(
            handler,
            "Agent checkout not available; pairing requires a running Hermes agent.",
            status=409,
        ) or True
    platform = str(body.get("platform") or "").lower().strip()
    code = str(body.get("code") or "").upper().strip()
    if not platform or not code:
        return bad(handler, "platform and code are required") or True
    try:
        with _scoped_agent_home(get_active_hermes_home()):
            store = _pairing_store()
            result = store.approve_code(platform, code)
            locked = store._is_locked_out(platform) if result is None else False
    except Exception:
        logger.exception("channels: pairing approve failed")
        return bad(handler, "Failed to approve pairing code", status=500) or True
    if result:
        j(handler, {"ok": True, "user": result})
        return True
    if locked:
        return bad(
            handler,
            f"Platform '{platform}' is locked out after too many failed approvals.",
            status=429,
        ) or True
    return bad(
        handler,
        f"Code '{code}' not found or expired for platform '{platform}'.",
        status=404,
    ) or True


def _handle_pairing_revoke(handler, body: dict) -> bool:
    if not _require_write_gate(handler):
        return True
    if not agent_available():
        return bad(
            handler,
            "Agent checkout not available; pairing requires a running Hermes agent.",
            status=409,
        ) or True
    platform = str(body.get("platform") or "").lower().strip()
    user_id = str(body.get("user_id") or "").strip()
    if not platform or not user_id:
        return bad(handler, "platform and user_id are required") or True
    try:
        with _scoped_agent_home(get_active_hermes_home()):
            store = _pairing_store()
            found = store.revoke(platform, user_id)
    except Exception:
        logger.exception("channels: pairing revoke failed")
        return bad(handler, "Failed to revoke pairing grant", status=500) or True
    if found:
        j(handler, {"ok": True})
        return True
    return bad(
        handler,
        f"User {user_id} not found in approved list for {platform}.",
        status=404,
    ) or True


def _handle_pairing_clear_pending(handler, body: dict) -> bool:
    if not _require_write_gate(handler):
        return True
    if not agent_available():
        return bad(
            handler,
            "Agent checkout not available; pairing requires a running Hermes agent.",
            status=409,
        ) or True
    platform = str(body.get("platform") or "").lower().strip() or None
    try:
        with _scoped_agent_home(get_active_hermes_home()):
            store = _pairing_store()
            count = store.clear_pending(platform)
    except Exception:
        logger.exception("channels: pairing clear-pending failed")
        return bad(handler, "Failed to clear pending pairing requests", status=500) or True
    j(handler, {"ok": True, "cleared": count})
    return True


def _handle_webhooks_enable(handler) -> bool:
    if not _require_write_gate(handler):
        return True
    if not agent_available():
        return bad(
            handler,
            "Agent checkout not available; webhooks require a running Hermes agent.",
            status=409,
        ) or True
    try:
        with _scoped_agent_home(get_active_hermes_home()):
            from hermes_cli.config import write_platform_config_field

            write_platform_config_field("webhook", "enabled", True)
    except Exception:
        logger.exception("channels: failed to enable webhook platform")
        return bad(handler, "Failed to enable the webhook platform", status=500) or True
    j(handler, {"ok": True, "enabled": True, "restart_required": True})
    return True


def _handle_webhook_create(handler, body: dict) -> bool:
    if not _require_write_gate(handler):
        return True
    if not agent_available():
        return bad(
            handler,
            "Agent checkout not available; webhooks require a running Hermes agent.",
            status=409,
        ) or True
    name = str(body.get("name") or "").strip().lower().replace(" ", "-")
    if not _WEBHOOK_NAME_RE.match(name):
        return bad(
            handler,
            "Invalid name. Use lowercase alphanumeric with hyphens/underscores.",
        ) or True
    events = [str(e).strip() for e in (body.get("events") or []) if str(e).strip()]
    deliver = str(body.get("deliver") or "log").strip() or "log"
    deliver_chat_id = str(body.get("deliver_chat_id") or "").strip()

    try:
        with _scoped_agent_home(get_active_hermes_home()):
            import secrets as _secrets

            import hermes_cli.webhook as wh

            if not wh._is_webhook_enabled():
                return bad(
                    handler,
                    "The webhook platform is not enabled. Enable it first.",
                    status=400,
                ) or True
            subs = wh._load_subscriptions()
            if name in subs:
                return bad(handler, f"A webhook named '{name}' already exists.", status=409) or True
            secret = _secrets.token_urlsafe(32)
            route: Dict[str, Any] = {
                "description": str(body.get("description") or f"WebUI-created subscription: {name}"),
                "events": events,
                "secret": secret,
                "deliver": deliver,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            if deliver_chat_id and deliver != "log":
                route["deliver_extra"] = {"chat_id": deliver_chat_id}
            subs[name] = route
            wh._save_subscriptions(subs)
            base_url = wh._get_webhook_base_url()
            summary = _webhook_summary(name, route, base_url)
    except Exception:
        logger.exception("channels: failed to create webhook %s", name)
        return bad(handler, "Failed to create webhook", status=500) or True

    # Secret is surfaced exactly once, on create.
    summary["secret"] = secret
    j(handler, summary)
    return True


def handle_channels_post(handler, parsed, body) -> bool:
    path = parsed.path
    if path == "/api/channels/pairing/approve":
        return _handle_pairing_approve(handler, body)
    if path == "/api/channels/pairing/revoke":
        return _handle_pairing_revoke(handler, body)
    if path == "/api/channels/pairing/clear-pending":
        return _handle_pairing_clear_pending(handler, body)
    if path == "/api/channels/webhooks/enable":
        return _handle_webhooks_enable(handler)
    if path == "/api/channels/webhooks":
        return _handle_webhook_create(handler, body)
    prefix = "/api/channels/"
    if path.startswith(prefix) and "/" not in path[len(prefix):]:
        platform_id = unquote(path[len(prefix):])
        return _handle_channel_platform_post(handler, platform_id, body)
    return False


# ── PUT handlers ─────────────────────────────────────────────────────────

_WEBHOOK_ENABLE_RE = re.compile(r"^/api/channels/webhooks/([^/]+)/enable$")


def _handle_webhook_set_enabled(handler, name: str, body: dict) -> bool:
    if not _require_write_gate(handler):
        return True
    if not agent_available():
        return bad(
            handler,
            "Agent checkout not available; webhooks require a running Hermes agent.",
            status=409,
        ) or True
    name = unquote(name).strip().lower()
    enabled = bool(body.get("enabled"))
    try:
        with _scoped_agent_home(get_active_hermes_home()):
            import hermes_cli.webhook as wh

            subs = wh._load_subscriptions()
            if name not in subs:
                return bad(handler, f"No subscription named '{name}'", status=404) or True
            subs[name]["enabled"] = enabled
            wh._save_subscriptions(subs)
    except Exception:
        logger.exception("channels: failed to toggle webhook %s", name)
        return bad(handler, "Failed to update webhook", status=500) or True
    j(handler, {"ok": True, "name": name, "enabled": enabled})
    return True


def handle_channels_put(handler, parsed, body) -> bool:
    match = _WEBHOOK_ENABLE_RE.match(parsed.path)
    if match:
        return _handle_webhook_set_enabled(handler, match.group(1), body)
    return False


# ── DELETE handlers ──────────────────────────────────────────────────────

def _handle_webhook_delete(handler, name: str) -> bool:
    if not _require_write_gate(handler):
        return True
    if not agent_available():
        return bad(
            handler,
            "Agent checkout not available; webhooks require a running Hermes agent.",
            status=409,
        ) or True
    name = unquote(name).strip().lower()
    try:
        with _scoped_agent_home(get_active_hermes_home()):
            import hermes_cli.webhook as wh

            subs = wh._load_subscriptions()
            if name not in subs:
                return bad(handler, f"No subscription named '{name}'", status=404) or True
            del subs[name]
            wh._save_subscriptions(subs)
    except Exception:
        logger.exception("channels: failed to delete webhook %s", name)
        return bad(handler, "Failed to delete webhook", status=500) or True
    j(handler, {"ok": True})
    return True


def handle_channels_delete(handler, parsed) -> bool:
    prefix = "/api/channels/webhooks/"
    if parsed.path.startswith(prefix):
        name = parsed.path[len(prefix):]
        if name and "/" not in name:
            return _handle_webhook_delete(handler, name)
    return False
