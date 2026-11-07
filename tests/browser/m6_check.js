// Headless browser check of M6 (the Memory section of Details), run by tests/test_browser_m6.py.
// Usage: node m6_check.js <launch-url> <other-project-id> [screenshot-dir]
const { chromium } = require('playwright');
const [url, otherProject, shots] = process.argv.slice(2);
const out = {};
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const pageErrors = [], cspErrors = [];
  page.on('pageerror', (e) => pageErrors.push(String(e)));
  page.on('console', (m) => { if (/Content Security Policy/.test(m.text())) cspErrors.push(m.text()); });
  const shot = async (name) => { if (shots) await page.screenshot({ path: `${shots}/${name}.png` }); };
  await page.goto(url);
  await page.waitForFunction(() => document.getElementById('rt-status').textContent === 'Connected');
  await page.click('#model-pill');
  await page.locator('.mm-row', { hasText: 'memory-aware:3b' }).locator('.mm-pick').click();
  await page.waitForSelector('#memory-body #use-memory');

  const items = () => page.$$eval('#memory-body .mem-item', (n) => n.map((x) => x.dataset.memId));
  const add = async (store, text, kind) => {
    const before = (await items()).length;
    const box = page.locator(`#memory-body input[data-mem-add="${store}"]`).locator('xpath=../..');
    await page.fill(`#memory-body input[data-mem-add="${store}"]`, text);
    await box.locator('select').selectOption(kind);
    await box.locator('button', { hasText: 'Add' }).click();
    await page.waitForFunction((n) => document.querySelectorAll('#memory-body .mem-item').length === n, before + 1);
  };
  const ask = async (text) => {
    const n = await page.$$eval('#thread .reply', (r) => r.length);
    await page.fill('#input', text);
    await page.press('#input', 'Enter');
    await page.waitForFunction((k) => {
      const r = document.querySelectorAll('#thread .reply');
      return r.length === k + 1 && !/answering|Waiting/.test(r[k].querySelector('.reply-meta').textContent);
    }, n);
    return page.$eval('#thread .reply:last-child', (r) => ({
      answer: r.querySelector('.answer').textContent,
      memory: (r.querySelector('.reply-mem') || {}).textContent || null,
    }));
  };

  await add('personal', 'Answer in British English', 'preference');
  await add('project', 'The release branch is called ship', 'decision');
  out.added = await page.$$eval('#memory-body .mem-item', (n) => n.map((x) => x.querySelector('.mem-tag').textContent));
  out.ids = await items();
  out.withMemory = await ask('What is the release branch?');
  out.usedNote = await page.$eval('#mem-used', (e) => e.textContent);
  await shot('m6-with-memory');

  await page.uncheck('#use-memory');
  out.withoutMemory = await ask('What is the release branch?');
  out.offNote = await page.$eval('#mem-used', (e) => e.textContent);
  await page.check('#use-memory');

  // edit the personal item
  const personal = page.locator(`#memory-body .mem-item[data-mem-id="${out.ids[0]}"]`);
  await personal.locator('button', { hasText: 'Edit' }).click();
  await personal.locator('input').fill('Answer in British English, briefly');
  await personal.locator('button', { hasText: 'Save' }).click();
  await page.waitForFunction((id) => /briefly/.test((document.querySelector(`[data-mem-id="${id}"] .mem-text`) || {}).textContent), out.ids[0]);

  // delete the project decision: two clicks
  const dec = page.locator(`#memory-body .mem-item[data-mem-id="${out.ids[1]}"]`);
  await dec.locator('button', { hasText: 'Delete' }).click();
  out.armed = await dec.locator('button.danger').textContent();
  await dec.locator('button.danger').click();
  await page.waitForFunction(() => document.querySelectorAll('#memory-body .mem-item').length === 1);
  out.afterDelete = await ask('What is the release branch?');
  await shot('m6-after-delete');

  // reopen the chat: the first reply still lists the ids it used
  const chatId = await page.$eval('#sec-sessions [aria-current="true"]', (n) => n.dataset.chatId);
  await page.click('[data-new]');
  await page.click(`#sec-sessions [data-chat-id="${chatId}"]`);
  await page.waitForFunction(() => document.querySelectorAll('#thread .reply').length === 3);
  out.reopened = await page.$$eval('#thread .reply', (r) => r.map((x) => ({
    answer: x.querySelector('.answer').textContent, memory: (x.querySelector('.reply-mem') || {}).textContent || null })));

  // another project: no project memory, the personal item applies (scope: all)
  await page.click(`#sec-projects [data-project-id="${otherProject}"]`);
  await page.waitForFunction(() => /This project: Other/.test(document.getElementById('memory-body').textContent));
  out.other = await page.$eval('#memory-body', (b) => ({
    items: [...b.querySelectorAll('.mem-item')].map((x) => x.dataset.memId), text: b.textContent }));
  out.otherReply = await ask('What is the release branch?');
  Object.assign(out, { pageErrors, cspErrors });
  await browser.close();
  console.log(JSON.stringify(out));
})().catch(async (e) => { console.error(e && e.stack || e); console.log(JSON.stringify(out)); process.exit(1); });
