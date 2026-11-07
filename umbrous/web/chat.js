// Real chat in the prototype page (M1), with saved history and projects (M2).
//
// Once runtime-bridge.js has a session it fires 'umbrous:connected'. This
// script then takes over the composer (replacing the prototype's fake run),
// the model pill (now a picker of installed Ollama models) and the Details
// panel's Session section. Since M2 it also fills the sidebar's Projects and
// Sessions from the runtime's history store, loads a saved chat when you click
// it, and offers Archive / Restore in the header's Options menu. Deleting is
// deliberately not available yet. Opened as a plain file, none of this runs and
// the prototype behaves exactly as before.
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
  let chatId = null;       // the open chat; null until the first message creates it (M2)
  let chatArchived = false;
  let projects = [];
  let projectId = 'prj_general';
  let showArchived = false;
  let view = 0;            // bumped on every chat switch, so a late answer for an old view is ignored
  let run = null;          // { runId, controller } while a reply is streaming
  let ollamaUp = false;
  let autoPicked = false;  // a default may be picked on the first load only; never again
  let downNotice = null;   // the single "Ollama is not running" notice with its Retry button

  // ---------- helpers ----------
  function ago(iso) {
    const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
    if (s < 60) return 'now';
    if (s < 3600) return Math.floor(s / 60) + 'm';
    if (s < 86400) return Math.floor(s / 3600) + 'h';
    return Math.floor(s / 86400) + 'd';
  }
  function projectName(id) {
    const p = projects.find((x) => x.id === id);
    if (p) return p.name;
    return id === 'prj_general' ? 'General' : 'this project';  // before the project list has loaded
  }
  async function getJson(path, opts) {
    const r = await api.fetch(path, opts);
    let j = null;
    try { j = await r.json(); } catch (_) { j = null; }
    if (!r.ok) throw new Error((j && (j.message || j.error)) || 'HTTP ' + r.status);
    return j;
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
    send.disabled = !($('#input').value.trim() && selected && ollamaUp && !chatArchived);
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
    if (!ollamaUp) {
      // Ollama is down: keep the chosen model as it is (it may come back), offer Retry.
      ollamaDown(j.error);
    } else {
      if (downNotice) { downNotice.remove(); downNotice = null; }
      if (selected && !models.some((m) => m.name === selected)) {
        // The chosen model disappeared: say so and wait for an explicit choice.
        notice(`The model ${selected} is no longer installed. Pick a model to continue.`, 'warn');
        selected = null;
      }
      // Only on the very first load is a default picked (shown in the pill before
      // anything is sent). After that, a missing choice stays missing until you pick.
      if (!selected && !autoPicked && models.length) {
        selected = (models.find((m) => m.tool_capable) || models[0]).name;
      }
    }
    autoPicked = true;
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
    ollamaUp = false;
    renderPill();
    if (downNotice && downNotice.isConnected) {          // one notice at a time, moved to the end
      downNotice.firstChild.textContent = message || 'Ollama is not running.';
      thread().appendChild(downNotice);
      scrollDown();
      return;
    }
    const n = notice('', 'bad');
    n.appendChild(el('span', '', message || 'Ollama is not running.'));
    const retry = el('button', 'pill retry', 'Retry');
    retry.type = 'button';
    retry.onclick = async () => { retry.disabled = true; await loadModels(); retry.disabled = false; };
    n.appendChild(retry);
    downNotice = n;
  }
  // ---------- history: sidebar (M2) ----------
  // Sidebar rows are .nav buttons like the prototype's, but each click handler
  // stops the event so the prototype's own .nav handler (which expects its
  // placeholder data attributes) never sees them.
  function navButton(text, n, cls) {
    const b = el('button', 'nav' + (cls ? ' ' + cls : ''));
    b.type = 'button';
    b.appendChild(el('span', 't', text));
    if (n !== undefined && n !== null) b.appendChild(el('span', 'n', String(n)));
    return b;
  }
  function markCurrent() {
    document.querySelectorAll('.nav').forEach((x) => x.removeAttribute('aria-current'));
    const p = document.querySelector(`#sec-projects [data-project-id="${projectId}"]`);
    if (p) p.setAttribute('aria-current', 'true');
    const c = chatId && document.querySelector(`#sec-sessions [data-chat-id="${chatId}"]`);
    if (c) c.setAttribute('aria-current', 'true');
    else if (!chatId) $('[data-new]').setAttribute('aria-current', 'true');
  }
  async function loadProjects() {
    const box = $('#sec-projects');
    try { projects = (await getJson('/api/projects')).projects; } catch (e) { box.textContent = ''; box.appendChild(el('div', 'proj-error', e.message)); return; }
    box.textContent = '';
    for (const p of projects) {
      const b = navButton(p.name, p.chats || '');
      b.prepend(folderIcon());
      b.dataset.projectId = p.id;
      b.title = `${p.chats} chat(s)` + (p.archived_chats ? `, ${p.archived_chats} archived` : '');
      b.addEventListener('click', (e) => { e.stopPropagation(); openProject(p.id); });
      box.appendChild(b);
    }
    markCurrent();
  }
  function icon(name) {
    const NS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('class', 'i');
    const use = document.createElementNS(NS, 'use');
    use.setAttribute('href', '#' + name);
    svg.appendChild(use);
    return svg;
  }
  function folderIcon() { return icon('folder'); }
  async function loadChats() {
    const box = $('#sec-sessions');
    const want = projectId;
    let active, archived = [];
    try {
      active = (await getJson('/api/chats?project_id=' + encodeURIComponent(want))).chats;
      if (showArchived) archived = (await getJson('/api/chats?archived=1&project_id=' + encodeURIComponent(want))).chats;
    } catch (e) { box.textContent = ''; box.appendChild(el('div', 'proj-error', e.message)); return; }
    if (want !== projectId) return;
    box.textContent = '';
    if (!active.length) {
      const p = projects.find((x) => x.id === want);
      const hidden = p ? p.archived_chats : archived.length;
      box.appendChild(el('div', 'side-empty', hidden
        ? `No active chats in ${projectName(want)}. ${hidden} archived.`
        : `No chats in ${projectName(want)} yet.`));
    }
    for (const c of active.concat(archived)) {
      const b = navButton(c.title, c.archived ? 'archived' : ago(c.updated_at), c.archived ? 'archived' : '');
      b.dataset.chatId = c.id;
      b.title = c.title;
      b.addEventListener('click', (e) => { e.stopPropagation(); openChat(c.id); });
      box.appendChild(b);
    }
    const p = projects.find((x) => x.id === want);
    if (showArchived || (p && p.archived_chats)) {
      const t = navButton(showArchived ? 'Hide archived' : `Show archived (${p ? p.archived_chats : 0})`, null, 'toggle-archived');
      t.dataset.toggleArchived = '';
      t.addEventListener('click', (e) => { e.stopPropagation(); showArchived = !showArchived; loadChats(); });
      box.appendChild(t);
    }
    markCurrent();
  }
  async function refreshSidebar() { await loadProjects(); await loadChats(); }

  function addProject() {
    const box = $('#sec-projects');
    box.hidden = false;
    $('[data-sec="projects"]').setAttribute('aria-expanded', 'true');
    if ($('#proj-input')) { $('#proj-input').focus(); return; }
    const input = el('input', 'proj-input');
    input.id = 'proj-input';
    input.placeholder = 'Project name, then Enter';
    input.maxLength = 60;
    const err = el('div', 'proj-error');
    err.hidden = true;
    const done = () => { input.remove(); err.remove(); };
    input.addEventListener('keydown', async (e) => {
      e.stopPropagation();
      if (e.key === 'Escape') { done(); return; }
      if (e.key !== 'Enter') return;
      e.preventDefault();
      try {
        const j = await getJson('/api/projects', { json: { name: input.value } });
        done();
        await loadProjects();
        openProject(j.project.id);
      } catch (ex) { err.textContent = ex.message; err.hidden = false; }
    });
    input.addEventListener('click', (e) => e.stopPropagation());
    box.prepend(input, err);
    input.focus();
  }

  function openProject(id) {
    projectId = id;
    showArchived = false;
    newChat();
    loadChats();
    loadAccess();
    loadMemory();
  }


  // ---------- tools, approvals and access (M3) ----------
  // The model only proposes a tool call. The runtime's gate decides, may ask
  // you here, runs it, and writes a receipt. Everything below shows what the
  // gate did; nothing in the page can run a tool itself. File names and search
  // words come from the model or your files, so they are shown as text only.
  const TOOL_ICON = { list_files: 'folder', read_file: 'doc', search_files: 'search' };
  const ACTION_TOOL = { 'fs.list': 'list_files', 'fs.read': 'read_file', 'fs.search': 'search_files' };
  const DECISION_WORD = { denied: 'Blocked', failed: 'Failed', uncertain: 'Unclear', cancelled: 'Not run' };
  function clip(s, n) { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; }
  function stepLabel(tool, args) {
    args = args || {};
    if (tool === 'read_file') return 'Read ' + clip(args.path || '?', 80) + (args.start_line ? ' from line ' + args.start_line : '');
    if (tool === 'list_files') return 'List ' + clip(args.path || 'the workspace folder', 80);
    if (tool === 'search_files') return 'Search for “' + clip(args.query || '', 60) + '”' + (args.path ? ' in ' + clip(args.path, 60) : '');
    return 'Unknown tool ' + clip(tool || '?', 40);
  }
  function stepRow(tool, label) {
    const row = el('div', 'step');
    row.appendChild(icon(TOOL_ICON[tool] || 'x'));
    row.appendChild(el('span', 't', label));
    const why = el('span', 'why');
    row.appendChild(why);
    return row;
  }
  function settleStep(row, decision, summary, notice) {
    if (decision !== 'allowed') {
      row.classList.add(decision);
      row.querySelector('use').setAttribute('href', '#x');
    }
    const word = DECISION_WORD[decision];
    row.querySelector('.why').textContent = '· ' + clip([word ? word + ': ' + summary : summary, notice].filter(Boolean).join(' '), 200);
  }
  function sourcesLine(evidence) {
    const files = [];
    for (const e of evidence || []) {
      if (e.kind !== 'file') continue;
      const where = e.ref + (e.lines ? (e.lines[0] === e.lines[1] ? ':' + e.lines[0] : ':' + e.lines[0] + '-' + e.lines[1]) : '');
      if (!files.includes(where)) files.push(where);
    }
    if (!files.length) return null;
    const line = el('div', 'sources', 'Sources: ');
    files.slice(0, 12).forEach((f, i) => {
      if (i) line.appendChild(document.createTextNode(', '));
      line.appendChild(el('code', '', f));
    });
    if (files.length > 12) line.appendChild(document.createTextNode(` and ${files.length - 12} more`));
    line.title = 'What the tools actually read for this reply, from the receipts. The answer text itself is not checked against them.';
    return line;
  }

  function approvalCard(ev) {
    const card = el('div', 'approval');
    card.dataset.approvalId = ev.approval_id;
    card.appendChild(el('div', 'ttl', 'Allow this read?'));
    const what = el('div', '', 'The model wants to ' + stepLabel(ev.tool, ev.args).replace(/^./, (c) => c.toLowerCase()) + '. ');
    card.appendChild(what);
    const scope = el('div', '');
    scope.appendChild(document.createTextNode('Action '));
    scope.appendChild(el('code', '', ev.action));
    scope.appendChild(document.createTextNode(' on '));
    scope.appendChild(el('code', '', ev.resource || '?'));
    card.appendChild(scope);
    if (ev.reason) card.appendChild(el('div', 'acc-note', ev.reason));
    const acts = el('div', 'acts');
    const status = el('div', 'acc-note');
    const choose = async (choice, label) => {
      acts.querySelectorAll('button').forEach((b) => { b.disabled = true; });
      try {
        await getJson('/api/approvals/' + encodeURIComponent(ev.approval_id), { json: { choice } });
        status.textContent = label;
      } catch (e) { status.textContent = 'This request is closed: ' + e.message; }
      card.classList.add('done');
      if (choice === 'grant') loadAccess();
    };
    const btn = (text, choice, label, cls) => {
      const b = el('button', cls || '', text);
      b.type = 'button';
      b.dataset.choice = choice;
      b.onclick = (e) => { e.stopPropagation(); choose(choice, label); };
      acts.appendChild(b);
    };
    btn('Approve once', 'once', 'Approved once, for exactly this request.', 'primary');
    btn('Allow ' + (ev.action === 'fs.read' ? 'reads' : ev.action === 'fs.list' ? 'listing' : 'searches') + ' in this project', 'grant',
        'Allowed for this project until you revoke it in Details › Access.');
    btn('Deny', 'deny', 'Denied. The model was told it may not do this.');
    card.append(acts, status);
    return card;
  }
  function closeApprovals(box, text) {
    box.querySelectorAll('.approval:not(.done)').forEach((c) => {
      c.classList.add('done');
      c.querySelectorAll('button').forEach((b) => { b.disabled = true; });
      c.querySelector('.acc-note:last-child').textContent = text;
    });
  }

  // ---------- M4: the task report under a reply, and its trace in Details ----------
  const TASK_WORD = { completed: 'Task completed', failed: 'Task failed', cancelled: 'Task stopped', unknown: 'Task outcome unknown' };
  function taskReport(rep) {
    if (!rep) return null;
    const box = el('div', 'task-report');
    box.dataset.status = rep.status;
    const a = rep.actions || {};
    const parts = [TASK_WORD[rep.status] || rep.status, rep.steps + (rep.steps === 1 ? ' step' : ' steps'),
      (a.completed || 0) + ' of ' + (rep.tool_calls || 0) + ' actions completed'];
    if (rep.code && rep.status !== 'completed') parts.push(rep.code.replace(/_/g, ' '));
    const head = el('div', 'tr-head', parts.join(' · '));
    const btn = el('button', 'linkish', 'Trace');
    btn.type = 'button';
    btn.title = 'Show every step and action of this task in Details';
    btn.onclick = (e) => { e.stopPropagation(); showTrace(rep.task_id); };
    head.append(' ', btn);
    box.appendChild(head);
    const list = (title, items, cls) => {
      if (!items.length) return;
      const d = el('div', 'tr-list ' + cls);
      d.appendChild(el('div', 'tr-title', title));
      items.slice(0, 8).forEach((t) => d.appendChild(el('div', 'tr-item', clip(t, 220))));
      if (items.length > 8) d.appendChild(el('div', 'tr-item', `and ${items.length - 8} more (see Trace)`));
      box.appendChild(d);
    };
    list('Did not complete', (rep.failures || []).map((f) => `${f.tool} ${f.resource}: ${f.status} (${f.code}) ${f.message || ''}`), 'fail');
    list('Not fully checked', rep.uncertainty || [], 'unc');
    if (rep.status === 'completed' && !(rep.failures || []).length && !(rep.uncertainty || []).length)
      box.appendChild(el('div', 'tr-item ok', rep.citations_checked
        ? `Every action completed, and the ${rep.citations_checked === 1 ? 'file:line citation' : rep.citations_checked + ' file:line citations'} in the answer ${rep.citations_checked === 1 ? 'is' : 'are'} covered by reads.`
        : 'Every action completed. The answer cites no file:line to check, so nothing it says was matched to a read.'));
    return box;
  }
  async function showTrace(taskId) {
    const sec = $('#trace-sec'), body = $('#trace-body');
    if (!sec || !body) return;
    const app = document.querySelector('.app');
    if (app && app.classList.contains('no-right')) { const t = $('#rtoggle'); if (t) t.click(); }
    sec.hidden = false;
    body.textContent = 'Loading…';
    sec.scrollIntoView({ block: 'start' });
    let t;
    try { t = await getJson('/api/tasks/' + encodeURIComponent(taskId)); }
    catch (e) { body.textContent = 'Could not load the trace: ' + e.message; return; }
    body.textContent = '';
    const tk = t.task;
    body.appendChild(el('div', 'acc-note', `${tk.task_id} · ${tk.status}${tk.code ? ' (' + tk.code + ')' : ''} · ${tk.model}`));
    const lim = tk.limits || {};
    body.appendChild(el('div', 'acc-note', `Limits: ${lim.max_steps} steps, ${lim.max_tool_calls} actions, ` +
      `${lim.tool_timeout_s}s per action, ${lim.task_timeout_s}s in all`));
    const t0 = t.events.length ? Date.parse(t.events[0].at) : 0;
    const ol = el('ol', 'trace-list');
    for (const e of t.events) {
      const li = el('li', 'trace-ev');
      li.dataset.kind = e.kind;
      const d = e.data || {};
      const bits = [];
      if (d.step != null) bits.push('step ' + d.step);
      if (d.tool) bits.push(d.tool + (d.resource ? ' ' + d.resource : ''));
      ['code', 'limit', 'decision', 'done_reason'].forEach((k) => { if (d[k]) bits.push(d[k]); });
      if (d.tool_calls != null && e.kind === 'step.completed') bits.push(d.tool_calls + ' call(s)');
      li.appendChild(el('code', '', '+' + ((Date.parse(e.at) - t0) / 1000).toFixed(2) + 's'));
      li.appendChild(el('span', 'k', ' ' + e.kind + ' '));
      li.appendChild(el('span', 'v', bits.join(' · ')));
      if (e.action_id) li.title = e.action_id + (d.receipt_id ? ' · ' + d.receipt_id : '');
      ol.appendChild(li);
    }
    body.appendChild(ol);
    body.appendChild(el('div', 'acc-note', 'The trace holds ids, statuses, paths and counts, never file contents, messages or search words.'));
  }
  async function tasksFor(id) {
    try { return (await getJson('/api/tasks?chat_id=' + encodeURIComponent(id))).tasks || []; }
    catch (_) { return []; }
  }

  // Stored chats: steps and sources come from the receipts, joined by task id.
  async function receiptsFor(id) {
    try { return (await getJson('/api/receipts?chat_id=' + encodeURIComponent(id))).receipts || []; }
    catch (_) { return []; }
  }
  function storedSteps(records) {
    const box = el('div', 'tool-steps');
    for (const r of records) {
      const tool = ACTION_TOOL[r.action] || 'unknown';
      const res = (r.scope || '').replace(/^project:\S+\s*/, '').replace(/^resource:/, '');
      const label = (tool === 'unknown' ? 'Unknown tool' : tool === 'read_file' ? 'Read ' : tool === 'list_files' ? 'List ' : 'Search in ') +
        (tool === 'unknown' ? '' : clip(res || '.', 80));
      const row = stepRow(tool, label);
      settleStep(row, r.decision, clip((r.reason || '').replace(/^[a-z_]+: /, ''), 160));
      row.title = r.receipt_id + ' · ' + r.started_at;
      box.appendChild(row);
    }
    return box;
  }

  // The Access section of the Details panel, for the selected project.
  let accessView = null;
  async function loadAccess() {
    const box = $('#access-body');
    if (!box) return;
    $('#access').hidden = false;
    const want = projectId;
    let v;
    try { v = await getJson('/api/projects/' + encodeURIComponent(want) + '/access'); }
    catch (e) { box.textContent = ''; box.appendChild(el('div', 'acc-err', e.message)); return; }
    if (want !== projectId) return;
    accessView = v;
    renderAccess(v);
  }
  async function accessPost(what, body, err) {
    try {
      const v = await getJson('/api/projects/' + encodeURIComponent(projectId) + '/' + what, { json: body || {} });
      accessView = v;
      renderAccess(v);
    } catch (e) { err.textContent = e.message; err.hidden = false; }
  }
  function renderAccess(v) {
    const box = $('#access-body');
    box.textContent = '';
    const err = el('div', 'acc-err');
    err.hidden = true;
    const ws = v.workspace;
    const profileSelect = (current) => {
      const sel = el('select');
      sel.setAttribute('aria-label', 'Access profile');
      for (const p of v.profiles) {
        const o = el('option', '', p.label);
        o.value = p.id;
        o.title = p.description;
        if (p.id === current) o.selected = true;
        sel.appendChild(o);
      }
      sel.addEventListener('click', (e) => e.stopPropagation());
      return sel;
    };
    const button = (text, fn) => {
      const b = el('button', '', text);
      b.type = 'button';
      b.onclick = (e) => { e.stopPropagation(); fn(b); };
      return b;
    };
    if (!ws) {
      box.appendChild(el('div', 'acc-note', `No workspace for ${projectName(v.project_id)}. Chats here cannot read files.`));
      const row = el('div', 'acc-row');
      const input = el('input');
      input.placeholder = 'Folder path, e.g. ~/code/notes';
      input.setAttribute('aria-label', 'Workspace folder');
      input.addEventListener('keydown', (e) => e.stopPropagation());
      input.addEventListener('click', (e) => e.stopPropagation());
      const sel = profileSelect('workspace_read');
      row.append(input);
      box.appendChild(row);
      const row2 = el('div', 'acc-row');
      row2.append(sel, button('Attach', () => accessPost('workspace', { root: input.value, profile: sel.value }, err)));
      box.appendChild(row2);
    } else {
      const root = el('div', 'acc-root', ws.root);
      root.title = 'Attached ' + ws.attached_at;
      box.appendChild(root);
      const row = el('div', 'acc-row');
      const sel = profileSelect(ws.profile);
      sel.onchange = () => accessPost('profile', { profile: sel.value }, err);
      row.append(sel, button('Detach', () => accessPost('workspace/detach', {}, err)));
      box.appendChild(row);
      const prof = v.profiles.find((p) => p.id === ws.profile);
      if (prof) box.appendChild(el('div', 'acc-note', prof.description));
      if (v.grants.length) {
        box.appendChild(el('div', 'acc-note', 'Allowed for this project:'));
        for (const g of v.grants) {
          const gr = el('div', 'acc-row');
          gr.dataset.grantId = g.id;
          gr.appendChild(el('span', '', g.capability));
          gr.firstChild.style.flex = '1';
          gr.appendChild(button('Revoke', () => accessPost('grants/' + encodeURIComponent(g.id) + '/revoke', {}, err)));
          box.appendChild(gr);
        }
      }
    }
    box.appendChild(err);
    box.appendChild(el('div', 'acc-note', 'Always blocked: ' + v.always_blocked));
    box.appendChild(el('div', 'acc-note', 'Read-only. This is a permission check inside Umbrous, not an operating-system sandbox.'));
  }

  // ---------- M6: memory ----------
  // Two stores the runtime keeps apart: personal memory (facts and
  // preferences, for every project or only some) and this project's memory
  // (decisions, state, lessons). Items you add here are retrieved for later
  // replies; each reply lists the ids it was given. Deleting an item stops it
  // being used from now on; replies already written are not changed.
  let memoryView = null, useMemory = true, lastMemory = null;
  const KIND_LABEL = { fact: 'fact', preference: 'preference', decision: 'decision', state: 'state', lesson: 'lesson' };
  function memoryLine(ids) {
    if (!ids || !ids.length) return null;
    const d = el('div', 'reply-mem', 'Memory used: ' + ids.join(', '));
    d.title = 'The memory items this reply was given. Deleting one later does not change this reply.';
    return d;
  }
  function showMemoryUsed(mem) {
    lastMemory = mem;
    const u = $('#mem-used');
    if (!u) return;
    if (!mem.enabled) u.textContent = 'Last reply: memory was off.';
    else if (!mem.used.length) u.textContent = 'Last reply: no memory items' + (mem.candidates ? ` (${mem.left_out} left out)` : '') + '.';
    else u.textContent = 'Last reply used: ' + mem.used.map((m) => m.id).join(', ') + (mem.left_out ? ` (${mem.left_out} left out by the limits)` : '');
  }
  async function loadMemory() {
    const box = $('#memory-body');
    if (!box) return;
    $('#memory').hidden = false;
    const want = projectId;
    let v;
    try { v = await getJson('/api/memory?project_id=' + encodeURIComponent(want)); }
    catch (e) { box.textContent = ''; box.appendChild(el('div', 'acc-err', e.message)); return; }
    if (want !== projectId) return;
    memoryView = v;
    renderMemory(v);
  }
  async function memoryPost(path, body, err) {
    try { await getJson(path, { json: body || {} }); await loadMemory(); }
    catch (e) { err.textContent = e.message; err.hidden = false; }
  }
  function memButton(text, fn, cls) {
    const b = el('button', cls || '', text);
    b.type = 'button';
    b.onclick = (e) => { e.stopPropagation(); fn(b); };
    return b;
  }
  function stopKeys(input) {
    input.addEventListener('keydown', (e) => e.stopPropagation());
    input.addEventListener('click', (e) => e.stopPropagation());
    return input;
  }
  function memItem(m, err, applies) {
    const row = el('div', 'mem-item' + (applies === false ? ' off' : ''));
    row.dataset.memId = m.id;
    const text = el('div', 'mem-text', m.text);
    let where = '';
    if (m.store === 'personal') where = m.scope === 'all' ? 'all projects' : m.scope.map(projectName).join(', ');
    text.appendChild(el('span', 'mem-tag', `${m.id} · ${KIND_LABEL[m.kind] || m.kind}${where ? ' · ' + where : ''}` +
      (applies === false ? ' · not used here' : '')));
    text.title = `Added ${m.created_at}` + (m.updated_at !== m.created_at ? `, edited ${m.updated_at}` : '') + ' · source: ' + m.source.type;
    const edit = memButton('Edit', () => {
      row.textContent = '';
      const input = stopKeys(el('input'));
      input.value = m.text;
      input.setAttribute('aria-label', 'Memory text');
      const r = el('div', 'acc-row');
      r.append(input, memButton('Save', () => memoryPost('/api/memory/' + m.id, { text: input.value }, err)),
        memButton('Cancel', () => renderMemory(memoryView)));
      row.appendChild(r);
      row.style.display = 'block';
      input.focus();
    });
    const del = memButton('Delete', (b) => {
      if (b.dataset.armed) { memoryPost('/api/memory/' + m.id + '/delete', {}, err); return; }
      b.dataset.armed = '1';
      b.textContent = 'Delete?';
      b.title = 'Click again to delete. It will not be retrieved for later replies. Chats that already used it keep their text, and in those chats that text still goes back to the model as history.';
      b.classList.add('danger');
    });
    row.append(text, edit, del);
    return row;
  }
  function addRow(store, kinds, err) {
    const wrap = el('div');
    const r = el('div', 'acc-row');
    const input = stopKeys(el('input'));
    input.placeholder = store === 'personal' ? 'Add a fact or preference' : 'Add a decision, state or lesson';
    input.setAttribute('aria-label', store === 'personal' ? 'New personal memory' : 'New project memory');
    input.dataset.memAdd = store;
    r.appendChild(input);
    const r2 = el('div', 'acc-row');
    const sel = el('select');
    sel.setAttribute('aria-label', 'Kind');
    for (const k of kinds) { const o = el('option', '', k); o.value = k; sel.appendChild(o); }
    sel.addEventListener('click', (e) => e.stopPropagation());
    r2.appendChild(sel);
    let only = null;
    if (store === 'personal') {
      const lab = el('label', 'acc-note');
      only = el('input');
      only.type = 'checkbox';
      lab.append(only, document.createTextNode(' only ' + projectName(projectId)));
      lab.style.margin = '0';
      r2.appendChild(lab);
    }
    r2.appendChild(memButton('Add', () => {
      const body = { store, text: input.value, kind: sel.value };
      if (store === 'project') body.project_id = projectId;
      else if (only && only.checked) body.scope = [projectId];
      memoryPost('/api/memory', body, err);
    }));
    wrap.append(r, r2);
    return wrap;
  }
  function renderMemory(v) {
    const box = $('#memory-body');
    box.textContent = '';
    const err = el('div', 'acc-err');
    err.hidden = true;
    const tog = el('label', 'acc-row');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.id = 'use-memory';
    cb.checked = useMemory;
    cb.onchange = () => { useMemory = cb.checked; };
    tog.append(cb, document.createTextNode(' Use memory for the next reply'));
    box.appendChild(tog);
    const used = el('div', 'acc-note');
    used.id = 'mem-used';
    box.appendChild(used);
    if (lastMemory) showMemoryUsed(lastMemory);
    box.appendChild(el('div', 'mem-h', 'Personal'));
    if (!v.personal.length) box.appendChild(el('div', 'acc-note', 'Nothing saved.'));
    for (const m of v.personal) box.appendChild(memItem(m, err, m.applies));
    box.appendChild(addRow('personal', ['fact', 'preference'], err));
    box.appendChild(el('div', 'mem-h', 'This project: ' + projectName(projectId)));
    if (!v.project.length) box.appendChild(el('div', 'acc-note', 'Nothing saved.'));
    for (const m of v.project) box.appendChild(memItem(m, err));
    box.appendChild(addRow('project', ['decision', 'state', 'lesson'], err));
    box.appendChild(err);
    box.appendChild(el('div', 'acc-note', `Each reply is given at most ${v.limits.max_items} items (about ` +
      `${v.limits.max_tokens} tokens), picked by shared words. Project memory never reaches another project. ` +
      'Turning memory off or deleting an item stops retrieval only: in a chat whose earlier replies repeat an ' +
      'item, those replies still go back to the model as history. To compare, use a new chat.'));
  }

  // ---------- history: the open chat ----------
  function setTitle(text) { $('#title').textContent = text; }
  function setComposer() {
    const input = $('#input');
    input.disabled = chatArchived;
    input.placeholder = chatArchived ? 'Archived chats are read-only. Restore it to continue.' : 'Message a local model…';
    syncSend();
  }
  function showSessionProject() { const sp = $('#sess-project'); if (sp) sp.textContent = projectName(projectId); }
  function clearContext() {  // the Context bar describes the next send; it is filled when a reply starts
    const label = $('#ctx-label'), bar = $('#ctx-bar'), row = $('#ctx-row');
    if (label) label.textContent = '–';
    if (bar) bar.style.width = '0%';
    if (row) row.title = '';
  }

  function newChat() {
    if (run) stop();
    view++;
    chatId = null;
    chatArchived = false;
    const t = thread();
    t.textContent = '';
    t.hidden = false;
    $('#empty').hidden = true;
    setTitle('New chat');
    setComposer();
    showSessionProject();
    clearContext();
    const n = el('div', 'notice');
    n.id = 'new-chat-notice';
    t.appendChild(n);
    newChatNotice();
    if (api && !ollamaUp) loadModels();
    markCurrent();
  }

  function newChatNotice() {
    const n = $('#new-chat-notice');
    if (n) n.textContent = `New chat in ${projectName(projectId)}. It talks to a model running in Ollama on this Mac; ` +
      'nothing leaves this machine. It is saved on this Mac once you send the first message.';
  }

  function renderStoredReply(m, receipts, tasks) {
    const reply = el('div', 'reply');
    const mine = m.task_id ? receipts.filter((r) => r.task_id === m.task_id) : [];
    if (mine.length) reply.appendChild(storedSteps(mine));
    const answer = el('p', 'answer plain', m.content);
    linkify(answer);
    const meta = el('div', 'reply-meta');
    const toks = m.prompt_tokens != null ? `${m.prompt_tokens} in · ${m.reply_tokens} out` : '';
    if (m.status === 'complete') meta.textContent = [m.model, toks].filter(Boolean).join(' · ');
    else {
      meta.textContent = [m.model, m.status === 'stopped' ? 'Stopped by you. Incomplete.' : 'Incomplete (' + m.status + ').'].filter(Boolean).join(' · ');
      meta.classList.add(m.status === 'stopped' ? 'warn' : 'bad');
    }
    reply.append(answer, meta);
    const src = sourcesLine([].concat(...mine.filter((r) => r.decision === 'allowed').map((r) => r.evidence)));
    if (src) reply.appendChild(src);
    const ml = memoryLine(m.memory_ids);
    if (ml) reply.appendChild(ml);
    const task = m.task_id && (tasks || []).find((x) => x.task_id === m.task_id);
    const tr = task && taskReport(task.report);
    if (tr) reply.appendChild(tr);
    return reply;
  }

  // M5: a task the runtime was stopped in the middle of (a crash, a forced
  // quit, a restart). Its message is shown with what was recorded, and Resume
  // runs it again under the current settings.
  function interruptedCard(task) {
    const t = thread();
    const u = el('div', 'msg-u', task.resume.text);
    u.style.marginTop = '48px';
    const reply = el('div', 'reply interrupted');
    reply.dataset.taskId = task.task_id;
    const head = el('div', 'notice warn small', 'Task interrupted: Umbrous stopped while this task was running, ' +
      'so its outcome is unknown. Nothing was replayed. ');
    const b = el('button', 'pill retry', 'Resume');
    b.type = 'button';
    b.dataset.act = 'resume';
    b.title = task.resume.message;
    b.onclick = () => { reply.remove(); u.remove(); send(task.resume.text, task.task_id); };
    head.appendChild(b);
    reply.appendChild(head);
    reply.appendChild(el('div', 'reply-meta', task.resume.message));
    const tr = taskReport(task.report);
    if (tr) reply.appendChild(tr);
    t.append(u, reply);
  }

  function archivedNotice() {
    const n = notice('This chat is archived. All its messages are kept and shown here read-only. Restore it to continue.', 'warn');
    n.id = 'archived-notice';
    const b = el('button', 'pill retry', 'Restore');
    b.type = 'button';
    b.onclick = () => setArchived(false);
    n.appendChild(b);
  }

  async function openChat(id) {
    if (run) stop();
    const mine = ++view;
    let j;
    try { j = await getJson('/api/chats/' + encodeURIComponent(id)); } catch (e) { notice('Could not open that chat: ' + e.message, 'bad'); return; }
    const hasTasks = j.messages.some((m) => m.task_id);
    const [receipts, tasks] = await Promise.all([hasTasks ? receiptsFor(id) : [], tasksFor(id)]);
    if (mine !== view) return;  // another chat was opened meanwhile
    const projectChanged = projectId !== j.chat.project_id;
    chatId = j.chat.id;
    chatArchived = j.chat.archived;
    projectId = j.chat.project_id;
    const t = thread();
    t.textContent = '';
    t.hidden = false;
    $('#empty').hidden = true;
    setTitle(j.chat.title);
    for (const m of j.messages) {
      if (m.role === 'user') {
        const u = el('div', 'msg-u', m.content);
        u.style.marginTop = '48px';
        t.appendChild(u);
      } else {
        t.appendChild(renderStoredReply(m, receipts, tasks));
        if (m.model) { const sm = $('#sess-model'); if (sm) sm.textContent = m.model; }
      }
    }
    const waiting = tasks.filter((x) => x.resume && x.resume.possible).pop();
    if (waiting && !chatArchived) interruptedCard(waiting);
    if (chatArchived) archivedNotice();
    setComposer();
    showSessionProject();
    if (projectChanged || !accessView) loadAccess();
    if (projectChanged || !memoryView) loadMemory();
    clearContext();
    markCurrent();
    scrollDown();
  }

  async function setArchived(archive) {
    if (!chatId) return;
    if (run) {  // stop the reply and wait until it has ended and been saved; the runtime refuses to archive before that
      const r = run;
      await stop();
      for (let i = 0; i < 100 && run === r; i++) await new Promise((ok) => setTimeout(ok, 50));
    }
    try { await getJson('/api/chats/' + encodeURIComponent(chatId) + (archive ? '/archive' : '/unarchive'), { json: {} }); }
    catch (e) { notice('Could not ' + (archive ? 'archive' : 'restore') + ' this chat: ' + e.message, 'bad'); return; }
    if (archive) showArchived = true;  // keep it visible in the list, marked archived
    await refreshSidebar();
    await openChat(chatId);
  }

  // ---------- header Options menu ----------
  function closeOptions() { const m = $('#opt-menu'); if (m) m.remove(); }
  function openOptions() {
    closeOptions();
    const menu = el('div', 'opt-menu');
    menu.id = 'opt-menu';
    const item = (label, sub, fn) => {
      const b = el('button', '', label);
      b.type = 'button';
      if (sub) b.appendChild(el('span', 'sub', sub));
      if (fn) b.onclick = (e) => { e.stopPropagation(); closeOptions(); fn(); };
      else b.disabled = true;
      menu.appendChild(b);
      return b;
    };
    if (!chatId) item('Archive chat', 'Nothing saved yet: send a message first.', null);
    else if (chatArchived) item('Restore chat', 'Back to the list; you can continue it.', () => setArchived(false)).dataset.act = 'restore';
    else item('Archive chat', 'Hidden from the list, read-only. Nothing is deleted.', () => setArchived(true)).dataset.act = 'archive';
    item('Delete chat', 'Not available yet: how deletion must work is still being decided.', null).dataset.act = 'delete';
    $('.panel .head').appendChild(menu);
  }

  async function send(q, resumeTask) {
    const t = thread();
    const u = el('div', 'msg-u');
    u.style.marginTop = '48px';
    u.textContent = q;
    t.appendChild(u);
    const reply = el('div', 'reply');
    const ctxNote = el('div', 'notice small');
    ctxNote.hidden = true;
    const steps = el('div', 'tool-steps');
    steps.hidden = true;
    const answer = el('p', 'answer plain');
    const meta = el('div', 'reply-meta', 'Waiting for ' + selected + '…');
    reply.append(ctxNote, steps, answer, meta);
    const pending = [];   // step rows waiting for their result, oldest first
    t.appendChild(reply);
    scrollDown();

    const controller = new AbortController();
    run = { runId: null, controller };
    syncSend();
    let finished = false;
    let memEl = null;   // M6: the memory ids this reply was given, shown from its start
    const mine = view;
    try {
      if (!chatId) {  // the chat is created on the first message, in the selected project
        const j = await getJson('/api/chats', { json: { project_id: projectId } });
        if (mine !== view) return;
        chatId = j.chat.id;
      }
      const body = { chat_id: chatId, model: selected, approvals: true };
      if (resumeTask) body.resume_task = resumeTask; else body.message = q;
      if (!useMemory) body.use_memory = false;
      const r = await api.fetch('/api/chat', { json: body, signal: controller.signal });
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
            meta.textContent = ev.model_requested + (ev.resume_of ? ' is answering again (resumed)…' : ' is answering…');
            showContext(ev.context);
            const notes = ev.context.notices.slice();
            if (ev.tools && ev.tools.off_reason) notes.push(ev.tools.off_reason);
            if (ev.memory) {
              showMemoryUsed(ev.memory);
              memEl = memoryLine(ev.memory.used.map((m) => m.id));
              if (memEl) meta.after(memEl);
            }
            if (notes.length) { ctxNote.textContent = notes.join(' '); ctxNote.hidden = false; }
          } else if (ev.type === 'tool_call') {
            const row = stepRow(ev.tool, stepLabel(ev.tool, ev.args));
            row.querySelector('.why').textContent = '· checking…';
            steps.appendChild(row);
            steps.hidden = false;
            pending.push(row);
            meta.textContent = 'Using the workspace…';
            scrollDown();
          } else if (ev.type === 'approval_required') {
            const row = pending[pending.length - 1];
            if (row) row.querySelector('.why').textContent = '· waiting for you';
            steps.appendChild(approvalCard(ev));
            meta.textContent = 'Waiting for your answer…';
            scrollDown();
          } else if (ev.type === 'tool_result') {
            closeApprovals(steps, 'Answered.');
            const row = pending.shift();
            if (row) { settleStep(row, ev.decision, ev.summary || ev.message, ev.notice); row.title = ev.receipt_id; }
            meta.textContent = selected + ' is answering…';
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
            const src = sourcesLine(ev.evidence);
            if (src) reply.appendChild(src);
            if (memEl) memEl.remove();
            memEl = memoryLine(ev.memory_ids);
            if (memEl) reply.appendChild(memEl);
            const tr = taskReport(ev.report);
            if (tr) reply.appendChild(tr);
          } else if (ev.type === 'error') {
            finished = true;
            if (answer.textContent) linkify(answer);
            if (ev.code === 'cancelled') meta.textContent = 'Stopped by you.' + (ev.partial ? ' The text above is incomplete.' : '');
            else meta.textContent = (ev.partial ? 'Incomplete: ' : 'Not answered: ') + ev.message;
            meta.classList.add(ev.code === 'cancelled' ? 'warn' : 'bad');
            // a partial reply is stored with its memory ids; one with no text is not stored at all
            if (memEl && !(ev.memory_ids && ev.memory_ids.length)) { memEl.remove(); memEl = null; }
            if (ev.code === 'ollama_unreachable') ollamaDown(ev.message);  // Retry button, same as at start
            if (ev.code === 'chat_archived') { chatArchived = true; archivedNotice(); setComposer(); }
            if (ev.report) {  // a task that ended early still reports what it did
              pending.splice(0).forEach((row) => settleStep(row, 'cancelled', 'Not run'));
              const tr = taskReport(ev.report);
              if (tr) reply.appendChild(tr);
            }
          }
        }
      }
      if (!finished) { meta.textContent = 'The connection to the runtime ended before the reply finished.'; meta.classList.add('bad'); }
      if (!finished && memEl && !answer.textContent.trim()) memEl.remove();
    } catch (e) {
      if (!finished && memEl && !answer.textContent.trim()) memEl.remove();   // nothing was kept
      if (!finished) {
        meta.textContent = controller.signal.aborted ? 'Stopped.' : 'Could not reach the Umbrous runtime (' + e.message + ').';
        meta.classList.add(controller.signal.aborted ? 'warn' : 'bad');
      }
    } finally {
      closeApprovals(steps, 'Closed: the reply ended.');
      run = null;
      syncSend();
      if (mine === view && chatId) {  // the title and order may have changed
        refreshSidebar().then(() => {
          const c = projects.length && document.querySelector(`#sec-sessions [data-chat-id="${chatId}"] .t`);
          if (c && mine === view) setTitle(c.textContent);
        });
      }
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
      if (!q || !selected || !ollamaUp || chatArchived) return;
      input.value = '';
      send(q);
    };
    sendBtn.addEventListener('click', (e) => { if (run) { e.preventDefault(); stop(); } });
    input.addEventListener('input', syncSend);   // runs after the prototype's own listener
    $('[data-new]').addEventListener('click', newChat);
    $('[data-addproj]').addEventListener('click', (e) => { e.stopPropagation(); addProject(); });
    $('#chat-options').addEventListener('click', (e) => {
      e.stopPropagation();
      if ($('#opt-menu')) closeOptions(); else openOptions();
    });
    $('#model-pill').addEventListener('click', (e) => {
      e.stopPropagation();
      if ($('#model-menu')) { closeMenu(); return; }
      openMenu();
      loadModels(true);  // refresh: Ollama may have come back, models may have changed
    });
    document.addEventListener('click', (e) => {
      if (!e.target.closest('#model-menu')) closeMenu();
      if (!e.target.closest('#opt-menu')) closeOptions();
    });
    $('#sec-projects').textContent = '';   // the prototype's placeholder projects and sessions
    $('#sec-sessions').textContent = '';
    $('[data-new]').click();  // the prototype's own New chat (sidebar state) plus ours
    refreshSidebar().then(() => { showSessionProject(); newChatNotice(); loadAccess(); loadMemory(); });
    loadModels();
  }

  document.addEventListener('umbrous:connected', init);
  if (window.umbrousApi) init();
})();
