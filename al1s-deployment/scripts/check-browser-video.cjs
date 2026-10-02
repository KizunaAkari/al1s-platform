// Real read-only video probe. No touch/key input, screenshots or message sending.
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.AL1S_PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const root = resolve(__dirname, '../..');
  const config = readFileSync(resolve(root, 'al1s-deployment/.env'), 'utf8');
  const match = config.match(/^AL1S_ADMIN_PASSWORD=(.*)$/m);
  if (!match) throw new Error('Admin password configuration missing');
  const password = match[1].trim().replace(/^(['"])(.*)\1$/, '$2');
  // Diagnostic only: this bypasses application proxy for this browser process,
  // not the machine's TUN routes. Normal acceptance leaves this unset.
  const browser = await chromium.launch({ headless: true,
    channel: process.env.AL1S_TEST_BROWSER_CHANNEL || undefined,
    args: [
      ...(process.env.AL1S_TEST_DIRECT_PROXY === '1' ? ['--no-proxy-server'] : []),
      ...(process.env.AL1S_TEST_RESOLVER ? ['--host-resolver-rules=' + process.env.AL1S_TEST_RESOLVER] : []),
    ],
  });
  let page;
  try {
    const context = await browser.newContext(process.env.AL1S_TEST_DIRECT_PROXY === '1' ? {
      proxy: { server: 'http://127.0.0.1:7897', bypass: 'rk3576-tronlong.local,localhost,127.0.0.1' },
    } : {});
    page = await context.newPage();
    await page.addInitScript(() => {
      if (typeof VideoDecoder === 'undefined') return;
      const configure = VideoDecoder.prototype.configure;
      VideoDecoder.prototype.configure = function(config) {
        globalThis.__al1sProbeCodec = config.codec;
        return configure.call(this, config);
      };
    });
    page.on('console', message => {
      const code = message.text().match(/(?:net::)?ERR_[A-Z_]+/);
      if (code) console.error('Browser network:', code[0]);
    });
    const diagnostic = await context.newCDPSession(page);
    await diagnostic.send('Network.enable');
    diagnostic.on('Network.webSocketFrameError', event => {
      console.error('WSS diagnostic:', event.errorMessage.replace(/\/scrcpy\/[^/\s'"]+/g, '/scrcpy/<redacted>').slice(0, 300));
    });
    page.on('websocket', socket => {
      console.log('Video endpoint:', new URL(socket.url()).origin);
      socket.on('socketerror', error => {
      const code = String(error).match(/(?:net::)?ERR_[A-Z_]+/);
      console.error('Video socket error:', code ? code[0] : 'handshake/transport failure');
      });
    });
    const origin = process.env.AL1S_TEST_ORIGIN || 'https://127.0.0.1:8443';
    await page.goto(origin + '/login');
    await page.locator('input[autocomplete=current-password]').fill(password);
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.waitForURL(origin + '/');
    await page.goto(origin + '/editor');
    await page.locator('.editor-devices button:not([disabled])').first().click();
    await page.getByRole('button', { name: '连接画面', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('.editor-video-actions')?.textContent.includes('只读画面'), null, { timeout: 45000 });
    const size = await page.locator('canvas[aria-label="手机实时画面"]').evaluate(canvas => [canvas.width, canvas.height]);
    assert(size[0] > 0 && size[1] > 0);
    console.log('PASS: authenticated editor creation, terminal acknowledgement, trusted WSS and decoded first frame', size.join('x'));
  } catch (error) {
    if (page) {
      const codec = await page.evaluate(() => globalThis.__al1sProbeCodec).catch(() => undefined);
      if (typeof codec === 'string' && /^avc1\.[a-fA-F0-9]{6}$/.test(codec)) console.error('Video codec:', codec);
      const alerts = await page.locator('.editor-video-panel .el-alert').allTextContents();
      if (alerts.length) console.error('Video UI:', alerts.join(' ').slice(0, 400));
    }
    throw error;
  } finally {
    if (page) {
      const close = page.getByRole('button', { name: '关闭会话', exact: true });
      if (await close.count()) await close.click().catch(() => {});
    }
    await browser.close();
  }
})().catch(error => { console.error(String(error.message).split('\n')[0]); process.exitCode = 1; });
