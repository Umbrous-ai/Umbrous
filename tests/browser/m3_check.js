// Headless browser check of M3 (tools, approvals, access), run by tests/test_browser_m3.py.
// Usage: node m3_check.js <launch-url> <workspace-folder> [screenshot-dir]
const { chromium } = require('playwright');
const [url, wsPath, shots] = process.argv.slice(2);
const out = {};
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const dialogs = [], cspErrors = [], pageErrors = [];
  page.on('dialog', (d) => { dialogs.push(d.message()); d.dismiss(); });
  page.on('console', (m) => { if (/Content Security Policy/.test(m.text())) cspErrors.push(m.text()); });
  page.on('pageerror', (e) => pageErrors.push(String(e)));
  const shot = async (name) => { if (shots) await page.screenshot({ path: `${shots}/${name}.png` }); };
  await page.goto(url);
  await page.waitForFunction(() => document.getElementById('rt-status').textContent === 'Connected');
  await page.waitForFunction(() => document.querySelector('#access-body .acc-note'));
  out.accessHidden = await page.$eval('#access', (e) => e.hidden);
  out.accessEmpty = await page.textContent('#access-body .acc-note');

  // verify the model's tool calls first, then pick it
  await page.click('#model-pill');
  const row = page.locator('.mm-row', { hasText: 'workspace-reader:3b' });
  await row.locator('.mm-check').click();
  await page.waitForFunction(() => [...document.querySelectorAll('.mm-row')].some((r) =>
    r.textContent.includes('workspace-reader:3b') && r.textContent.includes('Tool calls verified')));
  await page.locator('.mm-row', { hasText: 'workspace-reader:3b' }).locator('.mm-pick').click();

  // attach a workspace from the Access section
  await page.fill('#access-body input', wsPath);
  await page.click('#access-body button:has-text("Attach")');
  await page.waitForSelector('#access-body .acc-root');
  out.accessRoot = await page.textContent('#access-body .acc-root');
  out.profile = await page.$eval('#access-body select', (s) => s.value);

  const replies = () => page.$$eval('.reply', (r) => r.length);
  async function send(text) {
    const n = await replies();
    await page.fill('#input', text);
    await page.press('#input', 'Enter');
    await page.waitForFunction((n) => document.querySelectorAll('.reply').length === n + 1, n);
    return n;
  }
  async function finished(n) {
    await page.waitForFunction((n) => /·/.test(document.querySelectorAll('.reply .reply-meta')[n].textContent), n);
    await page.waitForTimeout(150);
    return page.$$eval('.reply', (r, n) => {
      const x = r[n];
      return {
        steps: [...x.querySelectorAll('.step')].map((s) => [s.className, s.querySelector('.t').textContent, s.querySelector('.why').textContent]),
        answer: x.querySelector('.answer').textContent,
        sources: x.querySelector('.sources') ? x.querySelector('.sources').textContent : null,
        approvals: [...x.querySelectorAll('.approval')].map((a) => a.querySelector('.acc-note:last-child').textContent),
      };
    }, n);
  }
  async function approve(n, choice) {
    await page.waitForFunction((n) => document.querySelectorAll('.reply')[n].querySelector('.approval:not(.done)'), n);
    const card = await page.$eval('.approval:not(.done)', (a) => ({ text: a.textContent, buttons: [...a.querySelectorAll('button')].map((b) => b.textContent) }));
    if (choice === 'once') await shot('m3-approval');
    await page.click(`.approval:not(.done) button[data-choice="${choice}"]`);
    return card;
  }
  const say = async (text) => finished(await send(text));

  out.read = await say('read notes/plan.md');
  await shot('m3-read');
  out.outside = await say('read ../outside/secret.txt');
  out.excluded = await say('read .ENV');
  out.symlink = await say('read link.txt');
  out.search = await say('where is the garden?');

  // ask before read: once, grant, deny
  await page.selectOption('#access-body select', 'ask_before_read');
  await page.waitForFunction(() => /Ask before every/.test(document.getElementById("access-body").textContent));
  let n = await send('read readme.txt');
  out.card = await approve(n, 'once');
  out.once = await finished(n);
  n = await send('read readme.txt');
  await approve(n, 'grant');
  out.granted = await finished(n);
  await page.waitForSelector('#access-body [data-grant-id]');
  out.grants = await page.$$eval('#access-body [data-grant-id]', (g) => g.map((x) => x.firstChild.textContent));
  await shot('m3-access');
  out.withGrant = await say('read notes/plan.md');                  // no card: the grant covers it
  await page.click('#access-body [data-grant-id] button');
  await page.waitForFunction(() => !document.querySelector('#access-body [data-grant-id]'));
  n = await send('read readme.txt');
  await approve(n, 'deny');
  out.denied = await finished(n);
  out.outsideUnderAsk = await say('read ../outside/secret.txt');    // still a hard block: no card

  // reopen the chat: steps and sources come back from the receipts
  await page.click('[data-new]');
  await page.click('#sec-sessions [data-chat-id]');
  await page.waitForFunction(() => document.querySelectorAll('#thread .reply').length >= 5);
  out.stored = await page.$$eval('#thread .reply', (r) => r.slice(0, 2).map((x) => ({
    steps: [...x.querySelectorAll('.step')].map((s) => [s.className, s.querySelector('.t').textContent]),
    sources: x.querySelector('.sources') ? x.querySelector('.sources').textContent : null,
  })));
  out.pageText = await page.textContent('body');

  // detach
  await page.click('#access-body button:has-text("Detach")');
  await page.waitForFunction(() => /No workspace/.test(document.getElementById('access-body').textContent));
  out.detached = true;
  Object.assign(out, { dialogs, cspErrors, pageErrors });
  await browser.close();
  console.log(JSON.stringify(out));
})().catch(async (e) => { console.error(e && e.stack || e); console.log(JSON.stringify(out)); process.exit(1); });
