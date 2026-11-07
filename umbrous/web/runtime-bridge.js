// Connects the prototype page to the local Umbrous runtime (M0).
//
// 1. bin/umbrous opens the page as http://127.0.0.1:<port>/#launch=<one-time code>.
// 2. This script removes the code from the address bar, then trades it once
//    at POST /api/session/claim for the session credential.
// 3. The credential is kept in sessionStorage (this tab only, gone when the tab
//    closes) and sent as X-Umbrous-Token on every API call.
// 4. Every 5 s it calls GET /api/health and fills in the Details panel's
//    Runtime section. Values are written with textContent, never as HTML.
(() => {
  'use strict';
  const KEY = 'umbrous.session';
  const POLL_MS = 5000;
  const $ = (id) => document.getElementById(id);

  function set(id, text) { const el = $(id); if (el) el.textContent = String(text); }
  function status(state, label, hint) {
    const dot = $('rt-dot'); if (dot) dot.dataset.state = state;
    set('rt-status', label);
    const row = $('rt-status-row'); if (row) row.title = hint || '';
  }
  function uptime(s) {
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
    return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
  }

  async function claim(code) {
    const r = await fetch('/api/session/claim', {
      method: 'POST', headers: { 'X-Umbrous-Launch': code }, cache: 'no-store', credentials: 'omit',
    });
    if (!r.ok) throw new Error('claim refused');
    return (await r.json()).token;
  }

  async function sessionToken() {
    const params = new URLSearchParams(location.hash.slice(1));
    const code = params.get('launch');
    if (code) {
      history.replaceState(null, '', location.pathname + location.search); // drop the code from the URL
      try {
        const token = await claim(code);
        sessionStorage.setItem(KEY, token);
        return token;
      } catch (_) { /* fall through: maybe this tab already has a session */ }
    }
    return sessionStorage.getItem(KEY);
  }

  async function poll(token) {
    try {
      const r = await fetch('/api/health', { headers: { 'X-Umbrous-Token': token }, cache: 'no-store', credentials: 'omit' });
      if (r.status === 401) {
        sessionStorage.removeItem(KEY);
        status('bad', 'Session ended', 'The runtime restarted or the credential is wrong. Relaunch with bin/umbrous.');
        return false;
      }
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const h = await r.json();
      status('ok', 'Connected', 'Health checked at ' + new Date().toLocaleTimeString());
      set('rt-version', h.version + ' · ' + h.milestone);
      set('rt-schema', 'v' + h.schema_version);
      set('rt-uptime', uptime(h.uptime_s));
      set('rt-bind', h.bind);
      return true;
    } catch (_) {
      status('bad', 'Unreachable', 'The runtime is not answering. Is bin/umbrous still running?');
      return true; // keep trying: it may come back
    }
  }

  async function start() {
    const token = await sessionToken();
    if (!token) {
      status('off', 'No session', 'Open Umbrous with bin/umbrous so the page receives a launch code.');
      return;
    }
    if (!(await poll(token))) return;
    const timer = setInterval(async () => { if (!(await poll(token))) clearInterval(timer); }, POLL_MS);
  }

  start();
})();
