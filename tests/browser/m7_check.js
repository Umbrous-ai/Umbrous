// Headless browser check of M7 (reviewing memory suggestions), run by tests/test_browser_m7.py.
// Usage: node m7_check.js <launch-url> [screenshot-dir]
const { chromium } = require('playwright');
const [url, shots] = process.argv.slice(2);
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
  await page.locator('.mm-row', { hasText: 'notes:3b' }).first().locator('.mm-pick').click();
  await page.waitForSelector('#memory-body #use-memory');
  const ask = async (text) => {
    const n = await page.$$eval('#thread .reply', (r) => r.length);
    await page.fill('#input', text);
    await page.press('#input', 'Enter');
    await page.waitForFunction((k) => {
      const r = document.querySelectorAll('#thread .reply');
      return r.length === k + 1 && !/answering|Waiting|Using|Looking/.test(r[k].querySelector('.reply-meta').textContent);
    }, n);
    return page.$eval('#thread .reply:last-child', (r) => ({
      answer: r.querySelector('.answer').textContent,
      memory: (r.querySelector('.reply-mem') || {}).textContent || null,
    }));
  };
  const panel = () => page.$eval('#memory-body', (b) => b.textContent);

  await ask('I prefer answers as short lists. We decided to release on Friday. What is here?');
  const card = page.locator('#thread .reply:last-child .mem-suggest');
  await card.waitFor();
  out.cards = await card.locator('.mem-cand').count();
  out.cardText = await card.textContent();
  await page.waitForFunction(() => /Waiting for review/.test(document.getElementById('memory-body').textContent));
  out.panelBefore = await panel();
  await shot('m7-suggested');

  // edit the decision, then accept it; reject the preference
  const dec = card.locator('.mem-cand', { hasText: 'decision' });
  await dec.locator('button', { hasText: 'Edit' }).click();
  await dec.locator('input').first().fill('Release on Friday, after the tests');
  await dec.locator('button', { hasText: 'Accept' }).click();
  await page.waitForFunction(() => /Saved as/.test(document.querySelector('#thread .reply:last-child .mem-suggest').textContent));
  out.accepted = (await card.locator('.acc-note.ok').textContent()).trim();
  const pref = card.locator('.mem-cand', { hasText: 'preference' });
  await pref.locator('button', { hasText: 'Reject' }).click();
  await page.waitForFunction(() => /Rejected/.test(document.querySelector('#thread .reply:last-child .mem-suggest').textContent));
  out.rejected = await card.textContent();
  await page.waitForFunction(() => !/Waiting for review/.test(document.getElementById('memory-body').textContent)
    && /Release on Friday, after the tests/.test(document.getElementById('memory-body').textContent));
  out.panelAfter = await panel();
  await shot('m7-reviewed');

  // a later reply in a new chat is given the accepted item
  await page.click('[data-new]');
  out.later = await ask('When do we release?');

  // a file's claim about you: offered only as the project lesson, the claim is not offered
  await page.click('[data-new]');
  await ask('read notes/claim.md');
  const card2 = page.locator('#thread .reply:last-child .mem-suggest');
  await card2.waitFor();
  out.claimCard = await card2.textContent();
  await shot('m7-file-claim');
  Object.assign(out, { pageErrors, cspErrors });
  await browser.close();
  console.log(JSON.stringify(out));
})().catch(async (e) => { console.error(e && e.stack || e); console.log(JSON.stringify(out)); process.exit(1); });
