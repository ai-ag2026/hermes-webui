"""Tests for api/channels.py — the Channels nav panel (platforms/pairing/webhooks).

Mirrors tests/test_agent_config_bridge.py's approach: a FakeAgent fixture
fakes hermes_constants / hermes_cli.config / hermes_cli.webhook / gateway.pairing
/ gateway.platform_registry in sys.modules so behavior is identical whether or
not a real hermes-agent checkout happens to be importable on the machine
running the tests.
"""

import io
import json
import sys
import types
from pathlib import Path
from typing import Any, cast

import pytest

import api.channels as channels


# ── Fake HTTP handler (mirrors tests/test_issue5057_moa_webui_route.py) ─────

class _Handler:
    def __init__(self):
        self.status = None
        self.response_headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        self.response_headers.append(("__end__", ""))


def _body(handler) -> dict:
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


def _parsed(path: str):
    return types.SimpleNamespace(path=path)


# ── FakeAgent: builds fake agent modules and records calls ─────────────────

class FakeAgent:
    def __init__(self):
        self.env_values: dict[str, str] = {}
        self.config_store: dict[str, Any] = {"platforms": {}}
        self.override_calls: list[tuple[str, Any]] = []
        # platform -> list of {"code": str, "user_id": str, "user_name": str, "age_minutes": int}
        self.pairing_pending: dict[str, list[dict]] = {}
        # platform -> {user_id: {"user_name": str}}
        self.pairing_approved: dict[str, dict[str, dict]] = {}
        self.webhook_enabled_flag = False
        self.webhook_subs: dict[str, dict] = {}
        self.plugin_platforms: list = []

        hermes_constants = types.ModuleType("hermes_constants")

        def set_hermes_home_override(path):
            self.override_calls.append(("set", str(path)))
            return object()

        def reset_hermes_home_override(token):
            self.override_calls.append(("reset", token))

        hermes_constants.set_hermes_home_override = set_hermes_home_override
        hermes_constants.reset_hermes_home_override = reset_hermes_home_override

        hermes_cli = types.ModuleType("hermes_cli")
        hermes_cli.__path__ = []  # mark as package

        config_mod = types.ModuleType("hermes_cli.config")
        config_mod.load_config = lambda: json.loads(json.dumps(self.config_store))

        def save_config(cfg, **kwargs):
            self.config_store = cfg

        config_mod.save_config = save_config

        def save_env_value(key, value):
            self.env_values[key] = value

        config_mod.save_env_value = save_env_value

        def write_platform_config_field(platform_key, field_key, value, **kwargs):
            platforms = self.config_store.setdefault("platforms", {})
            plat = platforms.setdefault(platform_key, {})
            plat[field_key] = value

        config_mod.write_platform_config_field = write_platform_config_field

        webhook_mod = types.ModuleType("hermes_cli.webhook")
        webhook_mod._is_webhook_enabled = lambda: self.webhook_enabled_flag
        webhook_mod._get_webhook_base_url = lambda: "http://localhost:8644"
        webhook_mod._load_subscriptions = lambda: dict(self.webhook_subs)

        def _save_subscriptions(subs):
            self.webhook_subs = dict(subs)

        webhook_mod._save_subscriptions = _save_subscriptions

        gateway_pkg = types.ModuleType("gateway")
        gateway_pkg.__path__ = []

        pairing_mod = types.ModuleType("gateway.pairing")
        outer = self

        class FakePairingStore:
            def list_pending(self, platform=None):
                results = []
                platforms = [platform] if platform else list(outer.pairing_pending)
                for p in platforms:
                    for entry in outer.pairing_pending.get(p, []):
                        results.append({"platform": p, **{k: v for k, v in entry.items() if k != "code"}})
                return results

            def list_approved(self, platform=None):
                results = []
                platforms = [platform] if platform else list(outer.pairing_approved)
                for p in platforms:
                    for uid, info in outer.pairing_approved.get(p, {}).items():
                        results.append({"platform": p, "user_id": uid, **info})
                return results

            def approve_code(self, platform, code):
                entries = outer.pairing_pending.get(platform, [])
                for i, entry in enumerate(entries):
                    if entry.get("code") == code:
                        del entries[i]
                        outer.pairing_approved.setdefault(platform, {})[entry["user_id"]] = {
                            "user_name": entry.get("user_name", "")
                        }
                        return {"user_id": entry["user_id"], "user_name": entry.get("user_name", "")}
                return None

            def _is_locked_out(self, platform):
                return False

            def revoke(self, platform, user_id):
                approved = outer.pairing_approved.get(platform, {})
                if user_id in approved:
                    del approved[user_id]
                    return True
                return False

            def clear_pending(self, platform=None):
                platforms = [platform] if platform else list(outer.pairing_pending)
                count = 0
                for p in platforms:
                    count += len(outer.pairing_pending.get(p, []))
                    outer.pairing_pending[p] = []
                return count

        pairing_mod.PairingStore = FakePairingStore

        registry_mod = types.ModuleType("gateway.platform_registry")

        class _PluginEntry:
            def __init__(self, name, label, required_env, install_hint=""):
                self.name = name
                self.label = label
                self.required_env = required_env
                self.install_hint = install_hint

        class _Registry:
            def plugin_entries(self_inner):
                return list(outer.plugin_platforms)

        registry_mod.platform_registry = _Registry()
        registry_mod._PluginEntry = _PluginEntry

        self.modules = {
            "hermes_constants": hermes_constants,
            "hermes_cli": hermes_cli,
            "hermes_cli.config": config_mod,
            "hermes_cli.webhook": webhook_mod,
            "gateway": gateway_pkg,
            "gateway.pairing": pairing_mod,
            "gateway.platform_registry": registry_mod,
        }


@pytest.fixture
def fake_agent(monkeypatch, tmp_path):
    fake = FakeAgent()
    for name, module in fake.modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(channels, "_AGENT_DIR", str(tmp_path / "agent"), raising=False)
    monkeypatch.setattr(channels, "_import_state", None, raising=False)
    monkeypatch.setattr(channels, "get_active_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(tmp_path / "config.yaml"))
    yield fake
    channels._import_state = None


@pytest.fixture
def no_agent(monkeypatch, tmp_path):
    """No agent checkout at all — standalone WebUI mode."""
    monkeypatch.setattr(channels, "_AGENT_DIR", None, raising=False)
    monkeypatch.setattr(channels, "_import_state", None, raising=False)
    monkeypatch.setattr(channels, "get_active_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(tmp_path / "config.yaml"))
    yield tmp_path
    channels._import_state = None


@pytest.fixture
def writable(monkeypatch):
    monkeypatch.setenv(channels.WRITE_GATE_ENV, "1")


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.delenv(channels.WRITE_GATE_ENV, raising=False)


# ── Probe ────────────────────────────────────────────────────────────────

class TestAgentProbe:
    def test_no_agent_dir_reports_unavailable(self, no_agent):
        assert channels.agent_available() is False

    def test_fake_agent_probes_ok(self, fake_agent):
        assert channels.agent_available() is True


# ── GET /api/channels ────────────────────────────────────────────────────

class TestChannelsGet:
    def test_standalone_catalog_is_mvp_only(self, no_agent, gated):
        handler = _Handler()
        channels.handle_channels_get(handler, _parsed("/api/channels"))
        body = _body(handler)
        assert handler.status == 200
        ids = [p["id"] for p in body["platforms"]]
        assert ids == ["telegram", "discord", "slack", "whatsapp"]
        assert body["agent_available"] is False
        assert body["writable"] is False

    def test_gate_closed_reports_not_writable(self, fake_agent, gated):
        handler = _Handler()
        channels.handle_channels_get(handler, _parsed("/api/channels"))
        body = _body(handler)
        assert body["writable"] is False
        assert body["write_gate_env"] == "HERMES_WEBUI_ALLOW_CHANNELS_WRITE"

    def test_gate_open_reports_writable(self, fake_agent, writable):
        handler = _Handler()
        channels.handle_channels_get(handler, _parsed("/api/channels"))
        body = _body(handler)
        assert body["writable"] is True

    def test_plugin_catalog_passthrough(self, fake_agent, gated):
        fake_agent.plugin_platforms.append(
            sys.modules["gateway.platform_registry"]._PluginEntry(
                "irc", "IRC", ("IRC_SERVER", "IRC_NICK"), "Connect to an IRC network."
            )
        )
        handler = _Handler()
        channels.handle_channels_get(handler, _parsed("/api/channels"))
        body = _body(handler)
        ids = [p["id"] for p in body["platforms"]]
        assert "irc" in ids
        irc = next(p for p in body["platforms"] if p["id"] == "irc")
        assert {f["key"] for f in irc["env_schema"]} == {"IRC_SERVER", "IRC_NICK"}

    def test_secrets_never_returned_in_get(self, fake_agent, gated):
        fake_agent.env_values["TELEGRAM_BOT_TOKEN"] = "super-secret-token"
        # write it to the fake .env-equivalent surface the standalone reader uses too
        (channels.get_active_hermes_home() / ".env").write_text(
            "TELEGRAM_BOT_TOKEN=super-secret-token\n", encoding="utf-8"
        )
        handler = _Handler()
        channels.handle_channels_get(handler, _parsed("/api/channels"))
        raw = handler.wfile.getvalue().decode("utf-8")
        assert "super-secret-token" not in raw
        body = _body(handler)
        telegram = next(p for p in body["platforms"] if p["id"] == "telegram")
        token_field = next(f for f in telegram["env_schema"] if f["key"] == "TELEGRAM_BOT_TOKEN")
        assert token_field["is_set"] is True
        assert token_field["masked_value"] == "••••••"
        assert telegram["configured"] is True


# ── POST /api/channels/{platform} ───────────────────────────────────────

class TestPlatformWrite:
    def test_write_gated_closed_returns_403(self, fake_agent, gated):
        handler = _Handler()
        channels.handle_channels_post(
            handler, _parsed("/api/channels/telegram"), {"enabled": True}
        )
        assert handler.status == 403
        assert channels.WRITE_GATE_ENV in _body(handler)["error"]

    def test_unknown_platform_404(self, fake_agent, writable):
        handler = _Handler()
        channels.handle_channels_post(
            handler, _parsed("/api/channels/nope"), {"enabled": True}
        )
        assert handler.status == 404

    def test_write_via_agent_bridge(self, fake_agent, writable):
        handler = _Handler()
        channels.handle_channels_post(
            handler,
            _parsed("/api/channels/telegram"),
            {"enabled": True, "env": {"TELEGRAM_BOT_TOKEN": "abc123"}},
        )
        assert handler.status == 200
        body = _body(handler)
        assert body["ok"] is True
        assert body["restart_required"] is True
        assert fake_agent.env_values["TELEGRAM_BOT_TOKEN"] == "abc123"
        assert fake_agent.config_store["platforms"]["telegram"]["enabled"] is True
        # scoped_agent_home was entered and exited around the write
        assert ("set", str(channels.get_active_hermes_home())) in fake_agent.override_calls

    def test_write_standalone_fallback(self, no_agent, writable):
        home = channels.get_active_hermes_home()
        handler = _Handler()
        channels.handle_channels_post(
            handler,
            _parsed("/api/channels/discord"),
            {"enabled": True, "env": {"DISCORD_BOT_TOKEN": "xyz789"}},
        )
        assert handler.status == 200
        env_text = (home / ".env").read_text(encoding="utf-8")
        assert "DISCORD_BOT_TOKEN=xyz789" in env_text
        cfg_text = (home / "config.yaml").read_text(encoding="utf-8")
        assert "discord" in cfg_text

    def test_unknown_env_key_rejected(self, fake_agent, writable):
        handler = _Handler()
        channels.handle_channels_post(
            handler,
            _parsed("/api/channels/telegram"),
            {"env": {"NOT_A_REAL_KEY": "x"}},
        )
        assert handler.status == 400

    def test_masked_placeholder_does_not_overwrite_secret(self, fake_agent, writable):
        fake_agent.env_values["TELEGRAM_BOT_TOKEN"] = "original-token"
        handler = _Handler()
        channels.handle_channels_post(
            handler,
            _parsed("/api/channels/telegram"),
            {"env": {"TELEGRAM_BOT_TOKEN": "••••••"}},
        )
        assert handler.status == 200
        assert fake_agent.env_values["TELEGRAM_BOT_TOKEN"] == "original-token"


# ── Pairing ───────────────────────────────────────────────────────────────

class TestPairing:
    def test_get_without_agent_reports_unavailable(self, no_agent):
        handler = _Handler()
        channels.handle_channels_get(handler, _parsed("/api/channels/pairing"))
        body = _body(handler)
        assert body == {"pending": [], "approved": [], "agent_available": False, "writable": False}

    def test_approve_mutates_store(self, fake_agent, writable):
        fake_agent.pairing_pending["telegram"] = [
            {"code": "ABCD1234", "user_id": "111", "user_name": "Alice", "age_minutes": 1}
        ]
        handler = _Handler()
        channels.handle_channels_post(
            handler,
            _parsed("/api/channels/pairing/approve"),
            {"platform": "telegram", "code": "abcd1234"},
        )
        assert handler.status == 200
        assert fake_agent.pairing_approved["telegram"]["111"]["user_name"] == "Alice"
        assert fake_agent.pairing_pending["telegram"] == []

    def test_approve_unknown_code_404(self, fake_agent, writable):
        handler = _Handler()
        channels.handle_channels_post(
            handler,
            _parsed("/api/channels/pairing/approve"),
            {"platform": "telegram", "code": "NOPE0000"},
        )
        assert handler.status == 404

    def test_approve_gated_closed_403(self, fake_agent, gated):
        handler = _Handler()
        channels.handle_channels_post(
            handler,
            _parsed("/api/channels/pairing/approve"),
            {"platform": "telegram", "code": "ABCD1234"},
        )
        assert handler.status == 403

    def test_revoke_mutates_store(self, fake_agent, writable):
        fake_agent.pairing_approved["telegram"] = {"111": {"user_name": "Alice"}}
        handler = _Handler()
        channels.handle_channels_post(
            handler,
            _parsed("/api/channels/pairing/revoke"),
            {"platform": "telegram", "user_id": "111"},
        )
        assert handler.status == 200
        assert "111" not in fake_agent.pairing_approved["telegram"]

    def test_clear_pending_mutates_store(self, fake_agent, writable):
        fake_agent.pairing_pending["telegram"] = [
            {"code": "AAAA1111", "user_id": "1", "user_name": "", "age_minutes": 1}
        ]
        fake_agent.pairing_pending["discord"] = [
            {"code": "BBBB2222", "user_id": "2", "user_name": "", "age_minutes": 1}
        ]
        handler = _Handler()
        channels.handle_channels_post(handler, _parsed("/api/channels/pairing/clear-pending"), {})
        assert handler.status == 200
        assert _body(handler)["cleared"] == 2
        assert fake_agent.pairing_pending["telegram"] == []
        assert fake_agent.pairing_pending["discord"] == []


# ── Webhooks ─────────────────────────────────────────────────────────────

class TestWebhooks:
    def test_get_without_agent_reports_unavailable(self, no_agent):
        handler = _Handler()
        channels.handle_channels_get(handler, _parsed("/api/channels/webhooks"))
        body = _body(handler)
        assert body["agent_available"] is False
        assert body["subscriptions"] == []

    def test_create_returns_secret_once_then_redacted(self, fake_agent, writable):
        fake_agent.webhook_enabled_flag = True
        handler = _Handler()
        channels.handle_channels_post(
            handler,
            _parsed("/api/channels/webhooks"),
            {"name": "github-push", "events": ["push"], "deliver": "log"},
        )
        assert handler.status == 200
        create_body = _body(handler)
        assert "secret" in create_body and create_body["secret"]
        assert create_body["secret_set"] is True

        handler2 = _Handler()
        channels.handle_channels_get(handler2, _parsed("/api/channels/webhooks"))
        list_body = _body(handler2)
        sub = next(s for s in list_body["subscriptions"] if s["name"] == "github-push")
        assert "secret" not in sub
        assert sub["secret_set"] is True

    def test_create_without_enable_fails(self, fake_agent, writable):
        handler = _Handler()
        channels.handle_channels_post(
            handler, _parsed("/api/channels/webhooks"), {"name": "x", "events": []}
        )
        assert handler.status == 400

    def test_create_gated_closed_403(self, fake_agent, gated):
        handler = _Handler()
        channels.handle_channels_post(
            handler, _parsed("/api/channels/webhooks"), {"name": "x", "events": []}
        )
        assert handler.status == 403

    def test_toggle_enabled(self, fake_agent, writable):
        fake_agent.webhook_enabled_flag = True
        fake_agent.webhook_subs["hook1"] = {"description": "", "events": [], "secret": "s", "deliver": "log"}
        handler = _Handler()
        channels.handle_channels_put(
            handler, _parsed("/api/channels/webhooks/hook1/enable"), {"enabled": False}
        )
        assert handler.status == 200
        assert fake_agent.webhook_subs["hook1"]["enabled"] is False

    def test_delete(self, fake_agent, writable):
        fake_agent.webhook_enabled_flag = True
        fake_agent.webhook_subs["hook1"] = {"description": "", "events": [], "secret": "s", "deliver": "log"}
        handler = _Handler()
        channels.handle_channels_delete(handler, _parsed("/api/channels/webhooks/hook1"))
        assert handler.status == 200
        assert "hook1" not in fake_agent.webhook_subs

    def test_delete_unknown_404(self, fake_agent, writable):
        fake_agent.webhook_enabled_flag = True
        handler = _Handler()
        channels.handle_channels_delete(handler, _parsed("/api/channels/webhooks/nope"))
        assert handler.status == 404


# ── Frontend markers ─────────────────────────────────────────────────────

class TestFrontendWiring:
    def _read(self, name: str) -> str:
        return (Path(__file__).resolve().parent.parent / "static" / name).read_text(encoding="utf-8")

    def test_panel_registered_in_main_view_panels(self):
        panels_js = self._read("panels.js")
        assert "'channels'" in panels_js.split("MAIN_VIEW_PANELS", 1)[1].split("\n", 1)[0]

    def test_titlebar_key_registered(self):
        panels_js = self._read("panels.js")
        assert "channels: 'tab_channels'" in panels_js

    def test_lazy_load_hook_registered(self):
        panels_js = self._read("panels.js")
        assert "await loadChannelsPanel()" in panels_js

    def test_nav_present_in_both_desktop_and_mobile(self):
        index_html = self._read("index.html")
        assert index_html.count('data-panel="channels"') == 2

    def test_main_view_container_present(self):
        index_html = self._read("index.html")
        assert 'id="mainChannels"' in index_html

    def test_showing_channels_css_rule_present(self):
        style_css = self._read("style.css")
        assert "main.main.showing-channels > #mainChannels" in style_css

    def test_i18n_keys_present_in_english_locale_only(self):
        i18n_js = self._read("i18n.js")
        en_block = i18n_js.split("const LOCALES", 1)[1]
        # First locale block is `en:`; slice up to the next top-level locale key.
        en_start = en_block.index("en: {")
        # crude but sufficient: find the next occurrence of a two-letter locale key
        # pattern at the same indentation used for `en:` and `de:` etc.
        next_locale = en_block.find("\n  it: {", en_start)
        assert next_locale > en_start, "could not bound the en locale block"
        en_only = en_block[en_start:next_locale]
        for key in ("tab_channels", "channels_tab_platforms", "channels_gate_notice", "channels_webhook_create"):
            assert key in en_only

    def test_script_tag_registered(self):
        index_html = self._read("index.html")
        assert 'src="static/channels.js' in index_html
