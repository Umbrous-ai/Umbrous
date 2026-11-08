'use strict';
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();
  const models = [
    { name: 'codex/a', display_name: 'Model A', provider: 'codex', reasoning_efforts: ['low', 'high'], default_reasoning_effort: 'low', speeds: ['standard', 'fast'], tool_capable: true },
    { name: 'codex/b', display_name: 'Model B', provider: 'codex', reasoning_efforts: ['medium'], default_reasoning_effort: 'medium', speeds: ['standard'], tool_capable: true },
  ];
  const sent = [];
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route('**/api/models', route => route.fulfill({ json: { reachable: true, models } }));
  await page.route('**/api/providers/codex', route => route.fulfill({ json: { available: true, authenticated: true, auth_type: 'chatgpt' } }));
  await page.route('**/api/providers/codex/login', route => route.fulfill({ json: { auth_url: 'https://auth.openai.com.evil.test/steal' } }));
  await page.route('**/api/chat', route => {
    const body = route.request().postDataJSON(); sent.push(body);
    return route.fulfill({ status: 200, contentType: 'application/x-ndjson', body: [
      { type: 'start', run_id: 'run_ui_test', model_requested: body.model, context: { estimated_tokens: 1, window: 8192, notices: [] } },
      { type: 'delta', text: 'Control settings received.' },
      { type: 'done', model: body.model, ms: 1 },
    ].map(x => JSON.stringify(x)).join('\n') + '\n' });
  });
  try {
    await page.goto(process.argv[2]);
    await page.waitForFunction(() => window.umbrousApi && document.querySelector('#model-pill-name').textContent === 'Pick a model');
    await page.locator('#model-pill').click();
    await page.locator('.mm-pick').filter({ hasText: 'Model A' }).click();
    await page.locator('#inference-controls').click();
    await page.locator('#reasoning-select').selectOption('high');
    await page.locator('#speed-select').selectOption('fast');
    await page.locator('#input').fill('hello');
    await page.locator('#send').click();
    await page.waitForFunction(() => document.querySelector('#thread').textContent.includes('Control settings received.'));
    assert.equal(sent[0].model, 'codex/a');
    assert.equal(sent[0].reasoning_effort, 'high');
    assert.equal(sent[0].speed, 'fast');
    await page.locator('#model-pill').click();
    await page.locator('.mm-pick').filter({ hasText: 'Model B' }).click();
    await page.locator('#inference-controls').click();
    assert.equal(await page.locator('#reasoning-select').inputValue(), 'medium');
    assert.equal(await page.locator('#speed-select').inputValue(), 'standard');
    assert.equal(await page.locator('#speed-select option[value="fast"]').evaluate(option => option.disabled), true);
    await page.locator('.mm-pick').filter({ hasText: 'Model A' }).click();
    await page.locator('#inference-controls').click();
    assert.equal(await page.locator('#reasoning-select').inputValue(), 'high');
    assert.equal(await page.locator('#speed-select').inputValue(), 'fast');
    const initialPages = context.pages().length;
    await page.locator('#connect-chatgpt').click();
    await page.waitForFunction(() => document.querySelector('#thread').textContent.includes('unexpected sign-in address'));
    assert.equal(context.pages().length, initialPages);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ controls: 'passed', cloudOptIn: 'explicit', unsupportedFast: 'disabled', unsafeLogin: 'blocked' }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
