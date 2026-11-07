// Headless browser check of M4 (task report under a reply, Trace in Details), run by tests/test_browser_m4.py.
// Usage: node m4_check.js <launch-url> <workspace-folder> [screenshot-dir]
const { chromium } = require('playwright');
const [url, wsPath, shots] = process.argv.slice(2);
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
  out.traceHiddenAtStart = await page.$eval('#trace-sec', (e) => e.hidden);
  out.placeholderGone = !(await page.textContent('#rpanel')).includes('Queried eval runs');

  await page.click('#model-pill');
  await page.locator('.mm-row', { hasText: 'multi-step:3b' }).locator('.mm-check').click();
  await page.waitForFunction(() => [...document.querySelectorAll('.mm-row')].some((r) =>
    r.textContent.includes('multi-step:3b') && r.textContent.includes('Tool calls verified')));
  await page.locator('.mm-row', { hasText: 'multi-step:3b' }).locator('.mm-pick').click();
  await page.waitForFunction(() => document.querySelector('#access-body input'));
  await page.fill('#access-body input', wsPath);
  await page.click('#access-body button:has-text("Attach")');
  await page.waitForSelector('#access-body .acc-root');

  await page.fill('#input', 'What is the plan and who owns it?');
  await page.press('#input', 'Enter');
  await page.waitForSelector('.reply .task-report');
  const live = () => page.$eval('.reply', (x) => ({
    steps: [...x.querySelectorAll('.step')].map((s) => s.querySelector('.t').textContent),
    head: x.querySelector('.task-report .tr-head').textContent,
    status: x.querySelector('.task-report').dataset.status,
    lists: [...x.querySelectorAll('.task-report .tr-list')].map((l) => [...l.children].map((c) => c.textContent)),
    sources: x.querySelector('.sources') ? x.querySelector('.sources').textContent : null,
  }));
  out.live = await live();
  await shot('m4-report');

  await page.click('.reply .task-report button');
  await page.waitForSelector('#trace-body .trace-list');
  out.trace = await page.$$eval('#trace-body .trace-ev .k', (k) => k.map((x) => x.textContent.trim()));
  out.traceText = await page.textContent('#trace-body');
  await shot('m4-trace');

  // reopen the chat: the report comes back from the stored task
  await page.click('[data-new]');
  await page.click('#sec-sessions [data-chat-id]');
  await page.waitForSelector('#thread .reply .task-report');
  out.stored = await live();
  Object.assign(out, { pageErrors, cspErrors });
  await browser.close();
  console.log(JSON.stringify(out));
})().catch(async (e) => { console.error(e && e.stack || e); console.log(JSON.stringify(out)); process.exit(1); });
