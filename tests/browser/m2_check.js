// Headless browser check of M2 history, run by tests/test_browser_m2.py.
// Usage: node m2_check.js <launch-url> <first|after-restart> [screenshot-dir]
//   first          make a project, chat in it, archive and restore a chat
//   after-restart  (a new runtime on the same data) everything is still there
const { chromium } = require('playwright');
const [url, mode, shots] = process.argv.slice(2);
const out = {};
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const dialogs = [], cspErrors = [], pageErrors = [];
  page.on('dialog', (d) => { dialogs.push(d.message()); d.dismiss(); });
  page.on('console', (m) => { if (/Content Security Policy/.test(m.text())) cspErrors.push(m.text()); });
  page.on('pageerror', (e) => pageErrors.push(String(e)));
  await page.goto(url);
  await page.waitForFunction(() => document.getElementById('rt-status').textContent === 'Connected');
  await page.waitForFunction(() => document.querySelector('#sec-projects [data-project-id="prj_general"]'));
  await page.waitForFunction(() => !/umbra-large|Pick/.test(document.getElementById('model-pill-name').textContent));
  await page.click('#model-pill');
  await page.click('.mm-pick:has-text("qwen2.5:3b")');
  const projects = () => page.$$eval('#sec-projects [data-project-id] .t', (n) => n.map((x) => x.textContent));
  const sessions = () => page.$$eval('#sec-sessions [data-chat-id] .t', (n) => n.map((x) => x.textContent));
  const clickProject = async (name) => {
    await page.click(`#sec-projects [data-project-id]:has-text("${name}")`);
    await page.waitForFunction((name) => document.getElementById('sess-project').textContent === name, name);
    await page.waitForTimeout(150);
  };
  async function say(text) {
    const n = await page.$$eval('.reply', (r) => r.length);
    await page.fill('#input', text);
    await page.press('#input', 'Enter');
    await page.waitForFunction((n) => document.querySelectorAll('.reply').length === n + 1
      && /·/.test(document.querySelectorAll('.reply .reply-meta')[n].textContent), n);
    await page.waitForTimeout(200);  // the sidebar refreshes after the reply
  }

  if (mode === 'first') {
    out.projectsAtStart = await projects();
    // add a project with the sidebar's + button
    await page.hover('[data-sec="projects"]');
    await page.click('[data-addproj]');
    await page.fill('#proj-input', 'Garden');
    await page.press('#proj-input', 'Enter');
    await page.waitForFunction(() => document.getElementById('sess-project').textContent === 'Garden');
    out.projectsAfterAdd = await projects();
    // a duplicate name is refused inline
    await page.click('[data-addproj]');
    await page.fill('#proj-input', 'garden');
    await page.press('#proj-input', 'Enter');
    await page.waitForFunction(() => !document.querySelector('.proj-error').hidden);
    out.duplicateError = await page.textContent('.proj-error');
    await page.press('#proj-input', 'Escape');

    out.sessionsBeforeSend = await sessions();
    await say('When should I plant tulips?');
    await say('And daffodils?');
    await page.click('[data-new]');
    await say('Best soil for roses');
    out.gardenSessions = await sessions();
    out.title = await page.textContent('#title');

    await clickProject('General');
    out.generalSessions = await sessions();
    out.generalEmptyText = await page.textContent('#sec-sessions');
    // a saved reply full of HTML is still inert when it is loaded back from the store
    await page.click('#model-pill');
    await page.click('.mm-pick:has-text("html-test:1b")');
    await say('show me html');
    await page.click('[data-new]');
    await page.click('#sec-sessions [data-chat-id]:has-text("show me html")');
    await page.waitForFunction(() => document.querySelectorAll('#thread .reply').length === 1);
    out.storedHtmlElements = await page.$$eval('#thread .reply .answer *', (n) => n.map((e) => e.tagName + ' ' + e.getAttribute('href')));
    await page.click('#model-pill');
    await page.click('.mm-pick:has-text("qwen2.5:3b")');

    // open the first chat again: the transcript comes back from the store
    await clickProject('Garden');
    await page.click('#sec-sessions [data-chat-id]:has-text("tulips")');
    await page.waitForFunction(() => document.getElementById('title').textContent.includes('tulips'));
    out.transcript = await page.$$eval('#thread .msg-u, #thread .reply .answer', (n) => n.map((x) => x.textContent));

    // archive it from the Options menu
    await page.click('#chat-options');
    out.optionItems = await page.$$eval('#opt-menu button', (b) => b.map((x) => (x.disabled ? '[off] ' : '') + x.firstChild.textContent));
    await page.click('#opt-menu [data-act="archive"]');
    await page.waitForSelector('#archived-notice');
    out.inputDisabledWhenArchived = await page.$eval('#input', (i) => i.disabled);
    out.sendDisabledWhenArchived = await page.$eval('#send', (b) => b.disabled);
    out.transcriptWhileArchived = await page.$$eval('#thread .msg-u', (n) => n.length);
    out.sessionsWithArchived = await page.$$eval('#sec-sessions [data-chat-id]', (n) => n.map((x) => x.textContent));
    if (shots) await page.screenshot({ path: shots + '/m2-archived.png' });
    // restore and continue
    await page.click('#archived-notice button');
    await page.waitForFunction(() => !document.getElementById('input').disabled);
    await say('And tulips in pots?');
    out.transcriptAfterRestore = await page.$$eval('#thread .msg-u', (n) => n.map((x) => x.textContent));
    // archive the rose chat and leave it archived, for the restart check
    await page.click('#sec-sessions [data-chat-id]:has-text("roses")');
    await page.waitForFunction(() => document.getElementById('title').textContent.includes('roses'));
    await page.click('#chat-options');
    await page.click('#opt-menu [data-act="archive"]');
    await page.waitForSelector('#archived-notice');
  } else {
    out.projects = await projects();
    await clickProject('Garden');
    out.sessions = await sessions();
    out.toggle = await page.textContent('#sec-sessions [data-toggle-archived]');
    await page.click('#sec-sessions [data-toggle-archived]');
    await page.waitForFunction(() => document.querySelector('#sec-sessions .nav.archived'));
    out.archivedSessions = await page.$$eval('#sec-sessions .nav.archived .t', (n) => n.map((x) => x.textContent));
    await page.click('#sec-sessions [data-chat-id]:has-text("tulips")');
    await page.waitForFunction(() => document.getElementById('title').textContent.includes('tulips'));
    out.transcript = await page.$$eval('#thread .msg-u', (n) => n.map((x) => x.textContent));
    out.sessProject = await page.textContent('#sess-project');
    if (shots) await page.screenshot({ path: shots + '/m2-history.png' });
  }
  out.dialogs = dialogs;
  out.cspErrors = cspErrors;
  out.pageErrors = pageErrors;
  console.log(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(String(e)); process.exit(1); });
