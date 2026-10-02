// Read-only UI smoke check; requires a local Playwright installation.
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.AL1S_PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const root = resolve(__dirname, '../..');
  const env = readFileSync(resolve(root, 'al1s-deployment/.env'), 'utf8');
  const match = env.match(/^AL1S_ADMIN_PASSWORD=(.*)$/m);
  if (!match) throw new Error('Administrator password configuration missing');
  const password = match[1].trim().replace(/^(['"])(.*)\1$/, '$2');
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ colorScheme: 'dark' });
    const page = await context.newPage();
    const origin = process.env.AL1S_TEST_ORIGIN || 'https://127.0.0.1:8443';
    await page.goto(origin + '/login');
    await page.waitForSelector('input[type=password]');
    assert.equal(await page.evaluate(() => getComputedStyle(document.body).backgroundColor), 'rgb(0, 0, 0)');
    const input = page.locator('input[autocomplete=current-password]');
    await input.fill(password);
    await page.locator('.el-input__password').click();
    assert.equal(await input.getAttribute('type'), 'text');
    await page.locator('.el-input__password').click();
    assert.equal(await input.getAttribute('type'), 'password');
    const response = page.waitForResponse(r => r.url().endsWith('/api/v1/auth/login') && r.request().method() === 'POST');
    await page.getByRole('button', { name: '登录', exact: true }).click();
    assert.equal((await response).status(), 200);
    await page.waitForURL(origin + '/');
    assert.equal(await page.evaluate(async () => (await fetch('/api/v1/auth/session')).status), 200);
    await page.emulateMedia({ colorScheme: 'light' });
    await page.waitForFunction(() => !document.documentElement.classList.contains('dark'));
    await page.emulateMedia({ colorScheme: 'dark' });
    await page.waitForFunction(() => document.documentElement.classList.contains('dark'));
    const foreign = await context.request.post(origin + '/api/v1/auth/logout', {
      headers: { Origin: 'https://untrusted.invalid', 'X-AL1S-CSRF': '1' },
    });
    assert.equal(foreign.status(), 403);
    assert.equal((await context.request.post(origin + '/api/v1/auth/logout', {
      headers: { Origin: origin, 'X-AL1S-CSRF': '1' },
    })).status(), 204);
    console.log('PASS: trusted TLS, browser login/session/logout, password visibility, system dark/light, foreign-origin rejection');
  } finally { await browser.close(); }
})().catch(error => { console.error(String(error.message).split('\n')[0]); process.exitCode = 1; });
