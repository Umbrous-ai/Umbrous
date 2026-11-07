// Real chat in the prototype page (M1).
//
// Once runtime-bridge.js has a session it fires 'umbrous:connected'. This
// script then takes over the composer (replacing the prototype's fake run),
// the model pill (now a picker of installed Ollama models) and the Details
// panel's Session section. Opened as a plain file, none of this runs and the
// prototype behaves exactly as before.
//
// Safe rendering: everything the model says is untrusted text. It is only ever
// written with textContent / text nodes, never parsed as HTML. After a reply
// finishes, web addresses become links that show their real address, open in
// a new tab with no referrer, and are never made for this machine's own
// addresses (so a reply cannot point the page at the Umbrous API).
(() => {
  'use strict';
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  };

  let api = null;
  let models = [];
  let selected = null;     // exact model name; never changed behind your back
  let chatId = null;
  let run = null;          // { runId, controller } while a reply is streaming
  let ollamaUp = false;

  // ---------- helpers ----------
  function newId() {
    const b = new Uint8Array(12);
    crypto.getRandomValues(b);
    return 'chat_' + Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
  }
  function fmtK(n) { return n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(n); }
  function scrollDown() { const s = $('#scroll'); if (s) s.scrollTop = 1e9; }
  function thread() { return $('#thread'); }

  const LOCAL_HOSTS = /^(localhost|127\.\d+\.\d+\.\d+|\[::1\]|0\.0\.0\.0)$/i;
  function linkify(node) {
    const text = node.textContent;
    node.textContent = '';
    const re = /https?:\/\/[^\s<>"'`]+/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      let raw = m[0].replace(/[.,;:!?)\]}]+$/, '');
      node.appendChild(document.createTextNode(text.slice(last, m.index)));
      let url = null;
      try { url = new URL(raw); } catch (_) { url = null; }
      if (url && (url.protocol === 'http:' || url.protocol === 'https:') && !LOCAL_HOSTS.test(url.hostname)
          && url.host !== location.host) {
        const a = el('a', 'ext', raw);  // the visible text is the real address
        a.href = url.href;
        a.title = url.href;
        a.target = '_blank';
        a.rel = 'noopener noreferrer nofollow';
        node.appendChild(a);
      } else {
        node.appendChild(document.createTextNode(raw));
      }
      last = m.index + raw.length;
      re.lastIndex = last;
    }
    node.appendChild(document.createTextNode(text.slice(last)));
  }

  // ---------- send / stop button ----------
  const ARROW = '#arrow-up', STOP = '#stop';
  function setIcon(href) {
    const use = $('#send use');
    if (use) use.setAttribute('href', href);
  }
  function syncSend() {
    const send = $('#send');
    if (!send) return;
    if (run) { send.disabled = false; send.title = 'Stop'; setIcon(STOP); return; }
    send.title = 'Send';
    setIcon(ARROW);
    send.disabled = !($('#input').value.trim() && selected && ollamaUp);
  }

  // ---------- Details panel ----------
  function showContext(ctx) {
    const label = $('#ctx-label'), bar = $('#ctx-bar');
    if (label) label.textContent = '~' + fmtK(ctx.estimated_tokens) + ' / ' + fmtK(ctx.window);
    if (bar) bar.style.width = Math.min(100, Math.round(100 * ctx.estimated_tokens / ctx.window)) + '%';
    const row = $('#ctx-row');
    if (row) row.title = `Estimate. ${ctx.turns_included} of ${ctx.turns_available} earlier messages sent ` +
      `(recent_turns ${ctx.recent_turns_limit}); ${ctx.reply_tokens} tokens kept for the reply.`;
  }

  // ---------- models ----------
  function modelLabel(m) {
    if (m.tool_capable) return 'Tool calls verified';
    if (m.probe) return 'Plain chat only';
    return 'Tool calls not checked';
  }
  function renderPill() {
    const name = $('#model-pill-name');
    if (!name) return;
    if (!ollamaUp) name.textContent = 'Ollama not running';
    else if (!models.length) name.textContent = 'No models installed';
    else name.textContent = selected || 'Pick a model';
    syncSend();
  }
  function closeMenu() { const m = $('#model-menu'); if (m) m.remove(); }
  function openMenu() {
    closeMenu();
    const menu = el('div', 'model-menu');
    menu.id = 'model-menu';
    if (!ollamaUp) menu.appendChild(el('div', 'mm-empty', 'Ollama is not running. Start it, then reopen this menu.'));
    else if (!models.length) menu.appendChild(el('div', 'mm-empty', 'No models installed. In a terminal: ollama pull qwen2.5:3b'));
    for (const m of models) {
      const row = el('div', 'mm-row' + (m.name === selected ? ' on' : ''));
      const pick = el('button', 'mm-pick');
      pick.type = 'button';
      pick.appendChild(el('span', 'mm-name', m.name));
      pick.appendChild(el('span', 'mm-sub', [m.parameter_size, m.context_length ? 'ctx ' + fmtK(m.context_length) : '', modelLabel(m)].filter(Boolean).join(' · ')));
      if (m.probe && !m.probe.passed) pick.title = m.probe.detail;
      pick.onclick = () => { selected = m.name; closeMenu(); renderPill(); };
      const check = el('button', 'mm-check', m.probe ? 'Re-check' : 'Check tools');
      check.type = 'button';
      check.title = 'Run a real tool-call round trip with a harmless test tool';
      check.onclick = async (e) => {
        e.stopPropagation();
        check.disabled = true;
        check.textContent = 'Checking…';
        try {
          const r = await api.fetch('/api/models/probe', { json: { model: m.name } });
          const j = await r.json();
          check.textContent = r.ok ? (j.passed ? 'Passed' : 'Failed') : 'Error';
          check.title = j.detail || j.message || '';
        } catch (_) { check.textContent = 'Error'; }
        await loadModels(true);
      };
      row.appendChild(pick);
      row.appendChild(check);
      menu.appendChild(row);
    }
    const foot = el('div', 'mm-foot', 'Only installed models are listed. Umbrous never switches models for you.');
    menu.appendChild(foot);
    $('#model-pill').after(menu);
  }

  async function loadModels(keepMenu) {
    let j;
    try {
      const r = await api.fetch('/api/models');
      j = await r.json();
    } catch (_) {
      j = { reachable: false, error: 'The Umbrous runtime is not answering.', models: [] };
    }
    ollamaUp = !!j.reachable;
    models = j.models || [];
    if (selected && !models.some((m) => m.name === selected)) {
      // The chosen model disappeared: say so, do not substitute another one.
      notice(`The model ${selected} is no longer installed. Pick another model.`, 'warn');
      selected = null;
    }
    if (!selected && models.length) selected = (models.find((m) => m.tool_capable) || models[0]).name;
    if (!ollamaUp) ollamaDown(j.error);
    renderPill();
    if (keepMenu && $('#model-menu')) openMenu();
  }

  // ---------- thread ----------
  function notice(text, kind) {
    const n = el('div', 'notice' + (kind ? ' ' + kind : ''), text);
    thread().appendChild(n);
    scrollDown();
    return n;
  }
  function ollamaDown(message) {
    const n = notice(message || 'Ollama is not running.', 'bad');
    const retry = el('button', 'pill retry', 'Retry');
    retry.type = 'button';
    retry.onclick = async () => { retry.disabled = true; await loadModels(); if (ollamaUp) n.remove(); else retry.disabled = false; };
    n.appendChild(retry);
  }
  function newChat() {
    if (run) stop();
    chatId = newId();
    const t = thread();
    t.textContent = '';
    t.hidden = false;
    $('#empty').hidden = true;
    $('#title').textContent = 'New chat';
    $('#input').placeholder = 'Message a local model…';
    t.appendChild(el('div', 'notice', 'Chat with a model running in Ollama on this Mac. Nothing leaves this machine. ' +
      'Chats are kept in memory only until history arrives in M2.'));
    if (api && !ollamaUp) loadModels();
  }

  async function send(q) {
    const t = thread();
    const u = el('div', 'msg-u');
    u.style.marginTop = '48px';
    u.textContent = q;
    t.appendChild(u);
    const reply = el('div', 'reply');
    const ctxNote = el('div', 'notice small');
    ctxNote.hidden = true;
    const answer = el('p', 'answer plain');
    const meta = el('div', 'reply-meta', 'Waiting for ' + selected + '…');
    reply.append(ctxNote, answer, meta);
    t.appendChild(reply);
    scrollDown();

    const controller = new AbortController();
    run = { runId: null, controller };
    syncSend();
    let finished = false;
    try {
      const r = await api.fetch('/api/chat', { json: { chat_id: chatId, model: selected, message: q }, signal: controller.signal });
      if (!r.ok || !r.body) throw new Error('HTTP ' + r.status);
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, i).trim();
          buf = buf.slice(i + 1);
          if (!line) continue;
          const ev = JSON.parse(line);
          if (ev.type === 'start') {
            run.runId = ev.run_id;
            meta.textContent = ev.model_requested + ' is answering…';
            showContext(ev.context);
            if (ev.context.notices.length) { ctxNote.textContent = ev.context.notices.join(' '); ctxNote.hidden = false; }
          } else if (ev.type === 'delta') {
            answer.appendChild(document.createTextNode(ev.text));
            scrollDown();
          } else if (ev.type === 'done') {
            finished = true;
            linkify(answer);
            const secs = (ev.ms / 1000).toFixed(1) + 's';
            const toks = ev.prompt_tokens != null ? `${ev.prompt_tokens} in (est. ${ev.estimated_prompt_tokens}) · ${ev.reply_tokens} out` : '';
            meta.textContent = [ev.model, toks, secs].filter(Boolean).join(' · ');
            meta.title = 'Exact model reported by Ollama for this reply';
            const sm = $('#sess-model'); if (sm) sm.textContent = ev.model;
          } else if (ev.type === 'error') {
            finished = true;
            if (answer.textContent) linkify(answer);
            if (ev.code === 'cancelled') meta.textContent = 'Stopped by you.' + (ev.partial ? ' The text above is incomplete.' : '');
            else meta.textContent = (ev.partial ? 'Incomplete: ' : 'Not answered: ') + ev.message;
            meta.classList.add(ev.code === 'cancelled' ? 'warn' : 'bad');
            if (ev.code === 'ollama_unreachable') { ollamaUp = false; renderPill(); }
          }
        }
      }
      if (!finished) { meta.textContent = 'The connection to the runtime ended before the reply finished.'; meta.classList.add('bad'); }
    } catch (e) {
      if (!finished) {
        meta.textContent = controller.signal.aborted ? 'Stopped.' : 'Could not reach the Umbrous runtime (' + e.message + ').';
        meta.classList.add(controller.signal.aborted ? 'warn' : 'bad');
      }
    } finally {
      run = null;
      syncSend();
    }
  }

  async function stop() {
    if (!run) return;
    const r = run;
    if (r.runId) {
      try { await api.fetch('/api/chat/cancel', { json: { run_id: r.runId } }); return; } catch (_) { /* fall through */ }
    }
    r.controller.abort();
  }

  // ---------- take over the prototype's composer ----------
  function init() {
    if (api) return;
    api = window.umbrousApi;
    const form = $('#composer'), input = $('#input'), sendBtn = $('#send');
    form.onsubmit = (e) => {           // replaces the prototype's fake agent run
      e.preventDefault();
      if (run) { stop(); return; }
      const q = input.value.trim();
      if (!q || !selected || !ollamaUp) return;
      input.value = '';
      send(q);
    };
    sendBtn.addEventListener('click', (e) => { if (run) { e.preventDefault(); stop(); } });
    input.addEventListener('input', syncSend);   // runs after the prototype's own listener
    $('[data-new]').addEventListener('click', newChat);
    $('#model-pill').addEventListener('click', (e) => { e.stopPropagation(); $('#model-menu') ? closeMenu() : openMenu(); });
    document.addEventListener('click', (e) => { if (!e.target.closest('#model-menu')) closeMenu(); });
    $('[data-new]').click();  // the prototype's own New chat (sidebar state) plus ours (fresh chat id)
    loadModels();
  }

  document.addEventListener('umbrous:connected', init);
  if (window.umbrousApi) init();
})();
