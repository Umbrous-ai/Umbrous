// Browser check: no automatic model switching, and recovery after Ollama goes down.
// Usage: node m1_recovery.js <launch-url> <fake-ollama-url>
const { chromium } = require('playwright');
const [url, fakeUrl] = process.argv.slice(2);
const control = (body) => fetch(fakeUrl + '/_control', { method: 'POST', body: JSON.stringify(body),
  headers: { 'Content-Type': 'application/json' } });
const out = {};
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto(url);
  await page.waitForFunction(() => document.getElementById('rt-status').textContent === 'Connected');
  await page.waitForFunction(() => !/umbra-large/.test(document.getElementById('model-pill-name').textContent));
  const pill = () => page.textContent('#model-pill-name');
  const pick = async (name) => {
    await page.click('#model-pill');
    await page.waitForSelector(`.mm-pick:has-text("${name}")`);
    await page.click(`.mm-pick:has-text("${name}")`);
  };

  // 1. The chosen model disappears: no substitute, an explicit choice is required.
  await pick('tiny-chat:1b');
  await control({ hidden: ['tiny-chat:1b'] });
  await page.click('#model-pill');                         // opening the picker refreshes the list
  await page.waitForFunction(() => /no longer installed/.test(document.getElementById('thread').textContent));
  await page.mouse.click(700, 300);
  out.pillAfterDisappear = await pill();
  await page.fill('#input', 'hello');
  out.sendDisabledAfterDisappear = await page.isDisabled('#send');

  // 2. Ollama goes down mid-chat: Retry appears, selection is kept, nothing switches.
  await pick('qwen2.5:3b');
  await control({ down: true });
  await page.fill('#input', 'are you there?');
  await page.press('#input', 'Enter');
  await page.waitForSelector('.notice.bad .retry');
  out.metaWhenDown = await page.textContent('.reply .reply-meta');
  out.pillWhenDown = await pill();

  // 3. Ollama comes back: Retry clears the notice, the same model is still selected, chat works.
  await control({ down: false });
  await page.click('.notice.bad .retry');
  await page.waitForFunction(() => !document.querySelector('.notice.bad'));
  out.pillAfterRetry = await pill();
  await page.fill('#input', 'back again');
  await page.press('#input', 'Enter');
  await page.waitForFunction(() => document.querySelectorAll('.reply').length === 2 &&
    /·/.test(document.querySelectorAll('.reply .reply-meta')[1].textContent));
  out.replyAfterRetry = await page.evaluate(() => document.querySelectorAll('.reply .answer')[1].textContent);
  console.log(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(String(e)); process.exit(1); });
