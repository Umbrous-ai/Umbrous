// Headless browser check of M5 (an interrupted task shown with Resume), run by tests/test_browser_m5.py.
// Usage: node m5_check.js <launch-url> <chat-id> [screenshot-dir]
const { chromium } = require('playwright');
const [url, chatId, shots] = process.argv.slice(2);
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
  await page.locator('.mm-row', { hasText: 'quick:3b' }).locator('.mm-pick').click();

  await page.click(`#sec-sessions [data-chat-id="${chatId}"]`);
  await page.waitForSelector('#thread .reply.interrupted');
  out.card = await page.$eval('#thread', (t) => ({
    user: t.querySelector('.msg-u').textContent,
    notice: t.querySelector('.reply.interrupted .notice').textContent,
    head: t.querySelector('.reply.interrupted .task-report .tr-head').textContent,
    status: t.querySelector('.reply.interrupted .task-report').dataset.status,
    hasResume: !!t.querySelector('.reply.interrupted [data-act="resume"]'),
  }));
  await shot('m5-interrupted');

  await page.click('.reply.interrupted [data-act="resume"]');
  await page.waitForSelector('#thread .reply .task-report[data-status="completed"]');
  out.resumed = await page.$eval('#thread', (t) => ({
    users: [...t.querySelectorAll('.msg-u')].map((u) => u.textContent),
    interruptedLeft: !!t.querySelector('.reply.interrupted'),
    lists: [...t.querySelectorAll('.task-report .tr-list')].map((l) => [...l.children].map((c) => c.textContent)),
  }));
  await shot('m5-resumed');

  // reopen: the card is gone, the stored exchange is shown
  await page.click('[data-new]');
  await page.click(`#sec-sessions [data-chat-id="${chatId}"]`);
  await page.waitForSelector('#thread .reply .task-report');
  out.reopened = await page.$eval('#thread', (t) => ({
    users: [...t.querySelectorAll('.msg-u')].map((u) => u.textContent),
    interrupted: !!t.querySelector('.reply.interrupted'),
  }));
  Object.assign(out, { pageErrors, cspErrors });
  await browser.close();
  console.log(JSON.stringify(out));
})().catch(async (e) => { console.error(e && e.stack || e); console.log(JSON.stringify(out)); process.exit(1); });
