// Headless browser check of M8 (selective start-fresh reset), run by tests/test_browser_m8.py.
// Usage: node m8_check.js <launch-url> <gone-chat-id> [screenshot-dir]
const { chromium } = require('playwright');
const [url, goneId, shots] = process.argv.slice(2);
const out = {};
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const pageErrors = [], cspErrors = [], posts = [];
  page.on('pageerror', (e) => pageErrors.push(String(e)));
  page.on('console', (m) => { if (/Content Security Policy/.test(m.text())) cspErrors.push(m.text()); });
  page.on('request', (r) => { if (r.method() === 'POST' && /\/api\/reset/.test(r.url())) posts.push(new URL(r.url()).pathname); });
  const shot = async (name) => { if (shots) await page.screenshot({ path: `${shots}/${name}.png` }); };
  const apiGet = (path) => page.evaluate(async (p) => (await window.umbrousApi.fetch(p)).status, path);
  await page.goto(url);
  await page.waitForFunction(() => document.getElementById('rt-status').textContent === 'Connected');
  await page.click('#model-pill');
  await page.locator('.mm-row', { hasText: 'notes:3b' }).first().locator('.mm-pick').click();
  await page.waitForSelector('#reset-body [data-act="reset-open"]');

  // Options › Delete chat… on the open chat: preselected, previewed at once
  await page.click(`#sec-sessions [data-chat-id="${goneId}"]`);
  await page.waitForFunction(() => document.querySelectorAll('#thread .msg-u').length === 1);
  await page.click('#chat-options');
  await page.click('#opt-menu [data-act="delete"]');
  await page.waitForSelector('#reset-preview .reset-preview');
  out.chatPreview = await page.textContent('#reset-preview');
  out.chatPreviewItems = await page.$$eval('#reset-preview [data-reset-item]', (r) => r.map((x) => x.dataset.resetItem));
  await shot('m8-preview-chat');
  // Cancel: nothing is sent, nothing changes
  await page.click('#reset-preview [data-act="reset-cancel"]');
  await page.waitForSelector('#reset-body [data-act="reset-open"]');
  out.afterCancel = { posts: posts.slice(), chat: await apiGet('/api/chats/' + goneId) };

  // A new preview: the chat and all personal memory
  await page.click('#reset-body [data-act="reset-open"]');
  await page.waitForSelector('#reset-body input[data-group="chats"]');
  await page.check(`#reset-body input[data-group="chats"][value="${goneId}"]`);
  await page.check('#reset-body input[data-group="personal"]');
  await page.click('#reset-body [data-act="reset-preview"]');
  await page.waitForSelector('#reset-preview .reset-preview');
  out.firstItems = await page.$$eval('#reset-preview [data-reset-item]', (r) => r.length);
  // something changes after the preview: a new personal item now matches "all"
  await page.evaluate(() => window.umbrousApi.fetch('/api/memory', { json: { store: 'personal', text: 'Likes cats', kind: 'preference' } }));
  await page.click('#reset-preview [data-act="reset-confirm"]');
  await page.waitForFunction(() => /changed since the preview/.test(document.getElementById('reset-preview').textContent));
  out.changed = await page.textContent('#reset-preview');
  out.changedItems = await page.$$eval('#reset-preview [data-reset-item]', (r) => r.length);
  out.chatAfterChange = await apiGet('/api/chats/' + goneId);
  await shot('m8-preview-changed');
  // confirm the new preview
  await page.click('#reset-preview [data-act="reset-confirm"]');
  await page.waitForSelector('#reset-done');
  out.done = await page.textContent('#reset-body');
  await page.waitForFunction(() => document.getElementById('title').textContent === 'New chat');
  out.sidebar = await page.textContent('#sec-sessions');
  await page.waitForFunction(() => !/Codename/.test(document.getElementById('memory-body').textContent));
  out.memory = await page.textContent('#memory-body');
  out.chatAfter = await apiGet('/api/chats/' + goneId);
  await shot('m8-done');
  // the fresh chat answers without the reset memory (the test checks the prompt)
  await page.fill('#input', 'What is the codename?');
  await page.press('#input', 'Enter');
  await page.waitForFunction(() => {
    const r = document.querySelector('#thread .reply:last-child .reply-meta');
    return r && !/answering|Waiting|Using|Looking/.test(r.textContent);
  });
  Object.assign(out, { pageErrors, cspErrors, posts });
  await browser.close();
  console.log(JSON.stringify(out));
})().catch(async (e) => { console.error(e && e.stack || e); console.log(JSON.stringify(out)); process.exit(1); });
