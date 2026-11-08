// Headless browser check of M9 (coding in a folder), run by tests/test_browser_m9.py.
// Usage: node m9_check.js <launch-url> <project-folder> [screenshot-dir]
const { chromium } = require('playwright');
const [url, proj, shots] = process.argv.slice(2);
const out = {};
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const pageErrors = [], cspErrors = [];
  page.on('pageerror', (e) => pageErrors.push(String(e)));
  page.on('console', (m) => { if (/Content Security Policy/.test(m.text())) cspErrors.push(m.text()); });
  const shot = async (name) => { if (shots) await page.screenshot({ path: `${shots}/${name}.png`, fullPage: true }); };
  await page.goto(url);
  await page.waitForFunction(() => document.getElementById('rt-status').textContent === 'Connected');
  await page.click('#model-pill');
  await page.locator('.mm-row', { hasText: 'coder:3b' }).locator('.mm-check').click();
  await page.waitForFunction(() => [...document.querySelectorAll('.mm-row')].some((r) =>
    r.textContent.includes('coder:3b') && r.textContent.includes('Tool calls verified')));
  await page.locator('.mm-row', { hasText: 'coder:3b' }).locator('.mm-pick').click();

  // 1. "work on <folder>": no folder attached before; the task attaches it, edits, tests, fixes, finishes
  await page.fill('#input', `work on ${proj}: add a multiply function to calc.py and make the tests pass`);
  await page.press('#input', 'Enter');
  await page.waitForSelector('.reply .task-report');
  const reply = (sel) => page.$$eval('.reply', (rs, sel) => {
    const x = rs[rs.length - 1];
    return {
      folder: x.querySelector('.folder-line') ? x.querySelector('.folder-line').textContent : null,
      steps: [...x.querySelectorAll('.step')].map((s) => s.querySelector('.t').textContent + ' ' + s.querySelector('.why').textContent),
      details: [...x.querySelectorAll('.step-detail summary')].map((s) => s.textContent),
      diffAdds: [...x.querySelectorAll('.step-detail .add')].map((s) => s.textContent),
      head: x.querySelector('.task-report .tr-head').textContent,
      lists: [...x.querySelectorAll('.task-report .tr-list')].map((l) => [...l.children].map((c) => c.textContent)),
      answer: x.querySelector('.answer').textContent,
    };
  });
  await page.click('.reply .step-detail summary >> nth=1');   // open the first test output
  out.first = await reply();
  out.firstOutputOpen = await page.$eval('.reply .step-detail >> nth=1', (d) => d.open && d.querySelector('pre').textContent);
  out.accessAfter = await page.textContent('#access-body');
  await shot('m9-coding-task');

  // 2. switch to "Code, ask before commands": the next command needs your approval
  await page.selectOption('#access-body select', 'code_ask_commands');
  await page.waitForFunction(() => document.querySelector('#access-body').textContent.includes('ask before each command'));
  await page.fill('#input', 'Run the tests again');
  await page.press('#input', 'Enter');
  await page.waitForSelector('.approval:not(.done)');
  out.approval = await page.$eval('.approval:not(.done)', (c) => c.textContent);
  await shot('m9-approval');
  await page.click('.approval:not(.done) button[data-choice="once"]');
  await page.waitForFunction(() => document.querySelectorAll('.reply .task-report').length === 2);
  out.second = await reply();
  Object.assign(out, { pageErrors, cspErrors });
  await browser.close();
  console.log(JSON.stringify(out));
})().catch(async (e) => { console.error(e && e.stack || e); console.log(JSON.stringify(out)); process.exit(1); });
