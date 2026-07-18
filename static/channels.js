// ── Channels panel: messaging platforms, pairing, webhooks ──
// Talks to /api/channels* (api/channels.py). See that module's docstring for
// the read/write model — reads are always open, writes are gated behind
// HERMES_WEBUI_ALLOW_CHANNELS_WRITE and disabled in the UI when the gate is off.

let _channelsData = null;       // last GET /api/channels payload
let _channelsPairingData = null;
let _channelsWebhooksData = null;
let _channelsActiveTab = 'platforms';

// ── Event delegation ──
// Row/card actions (save platform, revoke pairing, toggle/delete webhook) all
// carry values the WebUI does not control the character set of — pairing
// user_id comes verbatim from the messaging platform adapter, and a webhook
// name created via the CLI isn't constrained by the WebUI's own name regex.
// Interpolating those into an inline onclick="...('${esc(value)}')" string is
// NOT safe even with HTML-escaping: the browser HTML-decodes an attribute
// value before compiling it as the inline handler's JS source, so an
// HTML-escaped quote (&#39;) decodes right back to a real quote before the
// JS parser ever sees it, letting a value like `x');alert(1);//` break out
// of the string literal and inject a second statement. Values are instead
// carried in data-* attributes (a single, non-code context — HTML-escaping
// is fully sufficient there) and read via .dataset from a delegated
// listener bound once per container, never re-parsed as source.
function _bindChannelsDelegation(el, handlers) {
  if (!el || el.dataset.channelsBound === '1') return;
  el.dataset.channelsBound = '1';
  Object.keys(handlers).forEach(evt => el.addEventListener(evt, handlers[evt]));
}

function _channelsPlatformsClick(e) {
  const btn = e.target.closest('[data-channels-action="save-platform"]');
  if (btn) saveChannelsPlatform(btn.dataset.platformId);
}

function _channelsPairingClick(e) {
  const btn = e.target.closest('[data-channels-action="revoke-pairing"]');
  if (btn) revokeChannelsPairing(btn.dataset.platform, btn.dataset.userId);
}

function _channelsWebhooksClick(e) {
  const btn = e.target.closest('[data-channels-action="delete-webhook"]');
  if (btn) deleteChannelsWebhook(btn.dataset.webhookName);
}

function _channelsWebhooksChange(e) {
  const el = e.target;
  if (el && el.matches && el.matches('[data-channels-action="toggle-webhook"]')) {
    toggleChannelsWebhook(el.dataset.webhookName, el.checked);
  }
}

async function loadChannelsPanel() {
  const el = $('channelsPlatformsContent');
  if (!el) return;
  _bindChannelsDelegation(el, { click: _channelsPlatformsClick });
  el.innerHTML = `<div class="channels-empty">${esc(t('loading'))}</div>`;
  try {
    _channelsData = await api('/api/channels');
  } catch (e) {
    el.innerHTML = `<div class="channels-empty">${esc(t('channels_load_failed'))}</div>`;
    return;
  }
  _renderChannelsGateNotice(_channelsData);
  _renderChannelsPlatforms(_channelsData);
  if (_channelsActiveTab === 'pairing') await loadChannelsPairing();
  if (_channelsActiveTab === 'webhooks') await loadChannelsWebhooksTab();
}

function switchChannelsTab(tab) {
  _channelsActiveTab = tab;
  document.querySelectorAll('[data-channels-tab]').forEach(btn => {
    btn.classList.toggle('channels-tab-active', btn.dataset.channelsTab === tab);
  });
  document.querySelectorAll('[data-channels-pane]').forEach(pane => {
    pane.hidden = pane.dataset.channelsPane !== tab;
  });
  if (tab === 'pairing' && !_channelsPairingData) loadChannelsPairing();
  if (tab === 'webhooks' && !_channelsWebhooksData) loadChannelsWebhooksTab();
}

function _renderChannelsGateNotice(data) {
  const notice = $('channelsGateNotice');
  if (!notice) return;
  if (data && data.writable) {
    notice.hidden = true;
    notice.innerHTML = '';
    return;
  }
  const gateEnv = (data && data.write_gate_env) || 'HERMES_WEBUI_ALLOW_CHANNELS_WRITE';
  notice.hidden = false;
  notice.innerHTML = `${esc(t('channels_gate_notice'))} <code>${esc(gateEnv)}=1</code>`;
}

// ── Platforms tab ──

function _renderChannelsPlatforms(data) {
  const el = $('channelsPlatformsContent');
  if (!el) return;
  const platforms = (data && data.platforms) || [];
  const writable = !!(data && data.writable);
  if (!platforms.length) {
    el.innerHTML = `<div class="channels-empty">${esc(t('channels_no_platforms'))}</div>`;
    return;
  }
  el.innerHTML = platforms.map(p => _channelsPlatformCardHtml(p, writable)).join('');
}

function _channelsPlatformCardHtml(platform, writable) {
  const badge = platform.configured
    ? `<span class="provider-card-badge">${esc(t('channels_configured'))}</span>`
    : `<span class="provider-card-badge plugin-card-badge-disabled">${esc(t('channels_not_configured'))}</span>`;
  const fields = (platform.env_schema || []).map(f => _channelsEnvFieldHtml(platform.id, f, writable)).join('');
  const hint = platform.hint ? `<div class="provider-card-hint" style="margin-bottom:10px">${esc(platform.hint)}</div>` : '';
  const docs = platform.docs_url
    ? `<a href="${esc(platform.docs_url)}" target="_blank" rel="noopener noreferrer" style="font-size:11px">${esc(t('channels_docs_link'))}</a>`
    : '';
  return `
    <div class="provider-card" id="channelsCard-${esc(platform.id)}">
      <div class="provider-card-header" style="cursor:default">
        <div class="provider-card-info">
          <div class="provider-card-name">${esc(platform.name)}</div>
          <div class="provider-card-meta">${badge}</div>
        </div>
        <label class="plugin-toggle-switch has-tooltip" data-tooltip="${esc(t('channels_enabled'))}">
          <input type="checkbox" id="channelsEnabled-${esc(platform.id)}" ${platform.enabled ? 'checked' : ''} ${writable ? '' : 'disabled'}>
          <span class="plugin-toggle-slider"></span>
        </label>
      </div>
      <div class="provider-card-body" style="display:block">
        ${hint}
        ${fields}
        <div class="provider-card-row" style="margin-top:8px">
          <button type="button" class="provider-card-btn provider-card-btn-primary" data-channels-action="save-platform" data-platform-id="${esc(platform.id)}" ${writable ? '' : 'disabled'}>${esc(t('channels_save'))}</button>
          ${docs}
        </div>
      </div>
    </div>`;
}

function _channelsEnvFieldHtml(platformId, field, writable) {
  const inputId = `channelsEnv-${platformId}-${field.key}`;
  const placeholder = field.is_set ? (field.masked_value || t('channels_value_set')) : '';
  const type = field.secret ? 'password' : 'text';
  const requiredBadge = field.required ? ` <span style="color:var(--error)">*</span>` : '';
  return `
    <div class="provider-card-field" style="margin-bottom:10px">
      <label class="provider-card-label" for="${esc(inputId)}">${esc(field.label)}${requiredBadge}</label>
      <input class="provider-card-input" id="${esc(inputId)}" type="${type}" placeholder="${esc(placeholder)}" autocomplete="off" data-1p-ignore data-lpignore="true" ${writable ? '' : 'disabled'}>
    </div>`;
}

async function saveChannelsPlatform(platformId) {
  const platform = (_channelsData && _channelsData.platforms || []).find(p => p.id === platformId);
  if (!platform) return;
  const enabledEl = $(`channelsEnabled-${platformId}`);
  const env = {};
  (platform.env_schema || []).forEach(field => {
    const inputEl = $(`channelsEnv-${platformId}-${field.key}`);
    if (!inputEl) return;
    const val = inputEl.value;
    if (val === '' && field.is_set) return; // untouched — keep existing value
    if (val !== '') env[field.key] = val;
  });
  try {
    await api(`/api/channels/${encodeURIComponent(platformId)}`, {
      method: 'POST',
      body: JSON.stringify({ enabled: enabledEl ? !!enabledEl.checked : undefined, env }),
    });
    showToast(t('channels_platform_saved'));
    await loadChannelsPanel();
  } catch (e) {
    showToast(t('channels_save_failed') + (e && e.message ? e.message : ''), 4000);
  }
}

// ── Pairing tab ──

async function loadChannelsPairing() {
  const el = $('channelsPairingContent');
  if (!el) return;
  _bindChannelsDelegation(el, { click: _channelsPairingClick });
  el.innerHTML = `<div class="channels-empty">${esc(t('loading'))}</div>`;
  try {
    _channelsPairingData = await api('/api/channels/pairing');
  } catch (e) {
    el.innerHTML = `<div class="channels-empty">${esc(t('channels_load_failed'))}</div>`;
    return;
  }
  _renderChannelsPairing(_channelsPairingData);
}

function _renderChannelsPairing(data) {
  const el = $('channelsPairingContent');
  if (!el) return;
  const writable = !!(data && data.writable);
  if (!data || data.agent_available === false) {
    el.innerHTML = `<div class="channels-empty">${esc(t('channels_agent_unavailable'))}</div>`;
    return;
  }
  const pending = data.pending || [];
  const approved = data.approved || [];

  const pendingRows = pending.length
    ? pending.map(p => `
      <div class="channels-row">
        <div class="channels-row-info">
          <div class="channels-row-title">${esc(p.platform)} — ${esc(p.user_name || p.user_id || t('channels_unknown_user'))}</div>
          <div class="channels-row-meta">${esc(t('channels_pending_age', String(p.age_minutes)))}</div>
        </div>
      </div>`).join('')
    : `<div class="channels-empty">${esc(t('channels_no_pending'))}</div>`;

  const approvedRows = approved.length
    ? approved.map(a => `
      <div class="channels-row">
        <div class="channels-row-info">
          <div class="channels-row-title">${esc(a.platform)} — ${esc(a.user_name || a.user_id)}</div>
          <div class="channels-row-meta">${esc(a.user_id)}</div>
        </div>
        <button type="button" class="provider-card-btn provider-card-btn-danger" data-channels-action="revoke-pairing" data-platform="${esc(a.platform)}" data-user-id="${esc(a.user_id)}" ${writable ? '' : 'disabled'}>${esc(t('channels_revoke'))}</button>
      </div>`).join('')
    : `<div class="channels-empty">${esc(t('channels_no_approved'))}</div>`;

  el.innerHTML = `
    <div class="channels-section-title">${esc(t('channels_approve_code_title'))}</div>
    <div class="channels-hint">${esc(t('channels_approve_code_hint'))}</div>
    <div class="channels-form-row">
      <input class="provider-card-input" id="channelsApprovePlatform" type="text" placeholder="${esc(t('channels_platform_placeholder'))}" style="max-width:160px" ${writable ? '' : 'disabled'}>
      <input class="provider-card-input" id="channelsApproveCode" type="text" placeholder="${esc(t('channels_code_placeholder'))}" style="max-width:160px" ${writable ? '' : 'disabled'}>
      <button type="button" class="provider-card-btn provider-card-btn-primary" ${writable ? '' : 'disabled'} onclick="approveChannelsPairing()">${esc(t('channels_approve'))}</button>
    </div>
    <div class="channels-section-title">${esc(t('channels_pending_title'))}</div>
    ${pendingRows}
    <div class="provider-card-row" style="margin-top:8px">
      <button type="button" class="provider-card-btn provider-card-btn-danger" ${writable ? '' : 'disabled'} onclick="clearChannelsPending()">${esc(t('channels_clear_pending'))}</button>
    </div>
    <div class="channels-section-title">${esc(t('channels_approved_title'))}</div>
    ${approvedRows}`;
}

async function approveChannelsPairing() {
  const platformEl = $('channelsApprovePlatform');
  const codeEl = $('channelsApproveCode');
  const platform = platformEl ? platformEl.value.trim() : '';
  const code = codeEl ? codeEl.value.trim() : '';
  if (!platform || !code) {
    showToast(t('channels_platform_code_required'), 4000);
    return;
  }
  try {
    await api('/api/channels/pairing/approve', { method: 'POST', body: JSON.stringify({ platform, code }) });
    showToast(t('channels_pairing_approved'));
    await loadChannelsPairing();
  } catch (e) {
    showToast(t('channels_approve_failed') + (e && e.message ? e.message : ''), 4000);
  }
}

async function revokeChannelsPairing(platform, userId) {
  const ok = await showConfirmDialog({
    title: t('channels_revoke_confirm_title'),
    message: t('channels_revoke_confirm_message', userId),
    confirmLabel: t('channels_revoke'),
    danger: true,
    focusCancel: true,
  });
  if (!ok) return;
  try {
    await api('/api/channels/pairing/revoke', { method: 'POST', body: JSON.stringify({ platform, user_id: userId }) });
    showToast(t('channels_pairing_revoked'));
    await loadChannelsPairing();
  } catch (e) {
    showToast(t('channels_revoke_failed') + (e && e.message ? e.message : ''), 4000);
  }
}

async function clearChannelsPending() {
  const ok = await showConfirmDialog({
    title: t('channels_clear_pending_confirm_title'),
    message: t('channels_clear_pending_confirm_message'),
    confirmLabel: t('channels_clear_pending'),
    danger: true,
    focusCancel: true,
  });
  if (!ok) return;
  try {
    await api('/api/channels/pairing/clear-pending', { method: 'POST', body: JSON.stringify({}) });
    showToast(t('channels_pending_cleared'));
    await loadChannelsPairing();
  } catch (e) {
    showToast(t('channels_clear_pending_failed') + (e && e.message ? e.message : ''), 4000);
  }
}

// ── Webhooks tab ──

async function loadChannelsWebhooksTab() {
  const el = $('channelsWebhooksContent');
  if (!el) return;
  _bindChannelsDelegation(el, { click: _channelsWebhooksClick, change: _channelsWebhooksChange });
  el.innerHTML = `<div class="channels-empty">${esc(t('loading'))}</div>`;
  try {
    _channelsWebhooksData = await api('/api/channels/webhooks');
  } catch (e) {
    el.innerHTML = `<div class="channels-empty">${esc(t('channels_load_failed'))}</div>`;
    return;
  }
  _renderChannelsWebhooks(_channelsWebhooksData);
}

function _renderChannelsWebhooks(data) {
  const el = $('channelsWebhooksContent');
  if (!el) return;
  const writable = !!(data && data.writable);
  if (!data || data.agent_available === false) {
    el.innerHTML = `<div class="channels-empty">${esc(t('channels_agent_unavailable'))}</div>`;
    return;
  }
  if (!data.enabled) {
    el.innerHTML = `
      <div class="channels-hint">${esc(t('channels_webhooks_disabled_hint'))}</div>
      <button type="button" class="provider-card-btn provider-card-btn-primary" ${writable ? '' : 'disabled'} onclick="enableChannelsWebhookPlatform()">${esc(t('channels_webhooks_enable'))}</button>`;
    return;
  }
  const subs = data.subscriptions || [];
  const rows = subs.length
    ? subs.map(s => `
      <div class="channels-row">
        <div class="channels-row-info">
          <div class="channels-row-title">${esc(s.name)}</div>
          <div class="channels-row-meta">${esc(s.url)} · ${esc((s.events || []).join(', ') || t('channels_all_events'))} · ${esc(t('channels_deliver_to'))}: ${esc(s.deliver)}</div>
        </div>
        <label class="plugin-toggle-switch has-tooltip" data-tooltip="${esc(t('channels_enabled'))}">
          <input type="checkbox" data-channels-action="toggle-webhook" data-webhook-name="${esc(s.name)}" ${s.enabled ? 'checked' : ''} ${writable ? '' : 'disabled'}>
          <span class="plugin-toggle-slider"></span>
        </label>
        <button type="button" class="provider-card-btn provider-card-btn-danger" data-channels-action="delete-webhook" data-webhook-name="${esc(s.name)}" ${writable ? '' : 'disabled'}>${esc(t('channels_delete'))}</button>
      </div>`).join('')
    : `<div class="channels-empty">${esc(t('channels_no_webhooks'))}</div>`;

  el.innerHTML = `
    <div class="channels-section-title">${esc(t('channels_webhooks_title'))}</div>
    <div class="channels-hint">${esc(t('channels_webhooks_base_url'))}: <code>${esc(data.base_url)}</code></div>
    ${rows}
    <div class="channels-section-title">${esc(t('channels_webhook_create_title'))}</div>
    <div class="channels-form-row">
      <input class="provider-card-input" id="channelsWebhookName" type="text" placeholder="${esc(t('channels_webhook_name_placeholder'))}" style="max-width:200px" ${writable ? '' : 'disabled'}>
      <input class="provider-card-input" id="channelsWebhookEvents" type="text" placeholder="${esc(t('channels_webhook_events_placeholder'))}" ${writable ? '' : 'disabled'}>
    </div>
    <div class="channels-form-row">
      <select class="provider-card-input" id="channelsWebhookDeliver" style="max-width:160px" ${writable ? '' : 'disabled'}>
        <option value="log">${esc(t('channels_deliver_log'))}</option>
        ${(_channelsData && _channelsData.platforms || []).map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('')}
      </select>
      <input class="provider-card-input" id="channelsWebhookChatId" type="text" placeholder="${esc(t('channels_deliver_chat_id_placeholder'))}" style="max-width:200px" ${writable ? '' : 'disabled'}>
      <button type="button" class="provider-card-btn provider-card-btn-primary" ${writable ? '' : 'disabled'} onclick="createChannelsWebhook()">${esc(t('channels_webhook_create'))}</button>
    </div>
    <div id="channelsWebhookSecretReveal"></div>`;
}

async function enableChannelsWebhookPlatform() {
  try {
    await api('/api/channels/webhooks/enable', { method: 'POST', body: JSON.stringify({}) });
    showToast(t('channels_webhooks_enabled'));
    await loadChannelsWebhooksTab();
  } catch (e) {
    showToast(t('channels_save_failed') + (e && e.message ? e.message : ''), 4000);
  }
}

async function createChannelsWebhook() {
  const nameEl = $('channelsWebhookName');
  const eventsEl = $('channelsWebhookEvents');
  const deliverEl = $('channelsWebhookDeliver');
  const chatIdEl = $('channelsWebhookChatId');
  const name = nameEl ? nameEl.value.trim() : '';
  if (!name) {
    showToast(t('channels_webhook_name_required'), 4000);
    return;
  }
  const events = eventsEl ? eventsEl.value.split(',').map(s => s.trim()).filter(Boolean) : [];
  const deliver = deliverEl ? deliverEl.value : 'log';
  const deliverChatId = chatIdEl ? chatIdEl.value.trim() : '';
  try {
    const res = await api('/api/channels/webhooks', {
      method: 'POST',
      body: JSON.stringify({ name, events, deliver, deliver_chat_id: deliverChatId }),
    });
    showToast(t('channels_webhook_created'));
    await loadChannelsWebhooksTab();
    const reveal = $('channelsWebhookSecretReveal');
    if (reveal && res && res.secret) {
      reveal.innerHTML = `
        <div class="channels-secret-reveal">
          <div class="channels-hint">${esc(t('channels_secret_shown_once'))}</div>
          <code>${esc(res.secret)}</code>
        </div>`;
    }
  } catch (e) {
    showToast(t('channels_save_failed') + (e && e.message ? e.message : ''), 4000);
  }
}

async function toggleChannelsWebhook(name, enabled) {
  try {
    await api(`/api/channels/webhooks/${encodeURIComponent(name)}/enable`, {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    });
    showToast(t('channels_platform_saved'));
    await loadChannelsWebhooksTab();
  } catch (e) {
    showToast(t('channels_save_failed') + (e && e.message ? e.message : ''), 4000);
    await loadChannelsWebhooksTab();
  }
}

async function deleteChannelsWebhook(name) {
  const ok = await showConfirmDialog({
    title: t('channels_webhook_delete_confirm_title'),
    message: t('channels_webhook_delete_confirm_message', name),
    confirmLabel: t('channels_delete'),
    danger: true,
    focusCancel: true,
  });
  if (!ok) return;
  try {
    await api(`/api/channels/webhooks/${encodeURIComponent(name)}`, { method: 'DELETE' });
    showToast(t('channels_webhook_deleted'));
    await loadChannelsWebhooksTab();
  } catch (e) {
    showToast(t('channels_delete_failed') + (e && e.message ? e.message : ''), 4000);
  }
}
