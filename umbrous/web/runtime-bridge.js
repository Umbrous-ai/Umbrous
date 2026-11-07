// Connects the prototype page to the local Umbrous runtime (M0).
//
// 1. bin/umbrous opens the page as http://127.0.0.1:<port>/#launch=<one-time code>.
// 2. This script removes the code from the address bar, then trades it once
//    at POST /api/session/claim for the session credential.
// 3. The credential is kept only in this script's memory (a local variable),
//    never in sessionStorage, localStorage or cookies, which a browser writes to
//    disk. Reloading the page therefore ends the session: relaunch bin/umbrous.
//    It is sent as X-Umbrous-Token on every API call.
// 4. Every 5 s it calls GET /api/health and fills in the Details panel's
//    Runtime section. Values are written with textContent, never as HTML.
// 5. (M1) Other scripts on the page (chat.js) call the API through
//    window.umbrousApi.fetch(), which adds the credential itself. The
//    credential is never handed out, only used.
(() => {
  'use strict';
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
    if (!code) return null;
    history.replaceState(null, '', location.pathname + location.search); // drop the code from the URL
    try {
      return await claim(code); // held only by the caller's local variable
    } catch (_) {
      return null;
    }
  }

  async function poll(token) {
    try {
      const r = await fetch('/api/health', { headers: { 'X-Umbrous-Token': token }, cache: 'no-store', credentials: 'omit' });
      if (r.status === 401) {
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
      if (h.ollama) set('rt-ollama', h.ollama.reachable ? 'Running · ' + h.ollama.version : 'Not running');
      const ex = h.ollama_exposure;
      if (ex) {
        set('rt-exposure', ex.status === 'exposed' ? 'EXPOSED on network' : ex.status === 'loopback_only' ? 'Loopback only' : 'n/a');
        const row = $('rt-exposure-row');
        if (row) { row.title = ex.summary + '. ' + ex.limits; row.dataset.state = ex.status; }
      }
      return true;
    } catch (_) {
      status('bad', 'Unreachable', 'The runtime is not answering. Is bin/umbrous still running?');
      return true; // keep trying: it may come back
    }
  }

  function exposeApi(token) {
    const api = Object.freeze({
      fetch(path, opts = {}) {
        if (typeof path !== 'string' || !path.startsWith('/api/')) throw new Error('only /api/ paths');
        const headers = { 'X-Umbrous-Token': token };
        let body;
        if (opts.json !== undefined) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(opts.json); }
        return fetch(path, { method: opts.method || (body ? 'POST' : 'GET'), headers, body,
          signal: opts.signal, cache: 'no-store', credentials: 'omit' });
      },
    });
    Object.defineProperty(window, 'umbrousApi', { value: api, writable: false, configurable: false });
    document.dispatchEvent(new Event('umbrous:connected'));
  }

  async function start() {
    const token = await sessionToken();
    if (!token) {
      status('off', 'No session', 'Sessions do not survive a reload or a new tab. Relaunch with bin/umbrous.');
      return;
    }
    if (!(await poll(token))) return;
    exposeApi(token);
    const timer = setInterval(async () => { if (!(await poll(token))) clearInterval(timer); }, POLL_MS);
  }

  start();
})();
