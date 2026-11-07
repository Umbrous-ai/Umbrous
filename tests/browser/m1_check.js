// Headless browser check of the M1 page, run by tests/test_browser_m1.py when
// Node and Playwright are installed (it is skipped otherwise).
// Usage: node m1_check.js <launch-url> [screenshot-dir]
// Expects the stand-in Ollama from tests/fake_ollama.py (its demo models).
const { chromium } = require('playwright');
const url = process.argv[2];
const shots = process.argv[3];
const out = {};
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const dialogs = [];
  page.on('dialog', (d) => { dialogs.push(d.message()); d.dismiss(); });
  const cspErrors = [];
  page.on('console', (m) => { if (/Content Security Policy/.test(m.text())) cspErrors.push(m.text()); });
  const requests = [];
  page.on('request', (r) => requests.push(r.url()));
  await page.goto(url);
  await page.waitForFunction(() => document.getElementById('rt-status').textContent === 'Connected');
  await page.waitForFunction(() => !/umbra-large|Pick/.test(document.getElementById('model-pill-name').textContent));
  out.pill = await page.textContent('#model-pill-name');

  const replyCount = () => page.evaluate(() => document.querySelectorAll('.reply').length);
  async function ask(model, text) {
    await page.click('#model-pill');
    await page.click(`.mm-pick:has-text("${model}")`);
    const n = await replyCount();
    await page.fill('#input', text);
    await page.press('#input', 'Enter');
    await page.waitForFunction((n) => document.querySelectorAll('.reply').length === n + 1, n);
    return n;
  }
  const metaDone = (i) => page.waitForFunction((i) => /·|Stopped|Not sent|Incomplete/.test(
    document.querySelectorAll('.reply .reply-meta')[i].textContent), i);

  // 1. normal chat
  let i = await ask('qwen2.5:3b', 'hello from the browser');
  await metaDone(i);
  out.reply = await page.evaluate((i) => document.querySelectorAll('.reply .answer')[i].textContent, i);
  out.meta = await page.evaluate((i) => document.querySelectorAll('.reply .reply-meta')[i].textContent, i);
  out.sessModel = await page.textContent('#sess-model');
  out.ctx = await page.textContent('#ctx-label');

  // 2. HTML + script reply renders as inert text
  i = await ask('html-test:1b', 'show me html');
  await metaDone(i);
  out.htmlText = await page.evaluate((i) => document.querySelectorAll('.reply .answer')[i].textContent, i);
  out.htmlElements = await page.evaluate((i) => [...document.querySelectorAll('.reply .answer')[i].querySelectorAll('*')]
    .map((e) => e.tagName + (e.tagName === 'A' ? ' href=' + e.getAttribute('href') + ' text=' + e.textContent : '')), i);
  out.remoteRequests = requests.filter((u) => !u.startsWith('http://127.0.0.1:'));
  if (shots) await page.screenshot({ path: shots + '/m1-chat.png' });

  // 3. stop a slow reply
  i = await ask('slow-model:7b', 'take your time');
  await page.waitForFunction((i) => document.querySelectorAll('.reply .answer')[i].textContent.length > 10, i);
  out.sendTitleWhileRunning = await page.getAttribute('#send', 'title');
  await page.click('#send');
  await metaDone(i);
  out.stoppedMeta = await page.evaluate((i) => document.querySelectorAll('.reply .reply-meta')[i].textContent, i);
  out.sendTitleAfter = await page.getAttribute('#send', 'title');

  // 4. model menu labels after probing
  await page.click('#model-pill');
  await page.click('.mm-row:has-text("qwen2.5:3b") .mm-check');
  await page.waitForFunction(() => /Tool calls verified/.test(document.querySelector('#model-menu')?.textContent || ''));
  await page.click('.mm-row:has-text("tiny-chat:1b") .mm-check');
  await page.waitForFunction(() => /Plain chat only/.test(document.querySelector('#model-menu')?.textContent || ''));
  out.menu = await page.$$eval('.mm-row', (rows) => rows.map((r) => r.querySelector('.mm-name').textContent + ' | ' + r.querySelector('.mm-sub').textContent));
  if (shots) await page.screenshot({ path: shots + '/m1-models.png' });
  await page.mouse.click(700, 300);

  await page.waitForFunction(() => document.getElementById('rt-exposure').textContent !== '–');
  out.runtime = await page.$eval('#runtime', (e) => e.innerText.replace(/\n+/g, ' | '));
  out.dialogs = dialogs;
  out.cspErrors = cspErrors;
  console.log(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(String(e)); process.exit(1); });
