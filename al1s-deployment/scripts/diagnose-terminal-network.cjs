// Bounded handshake-only diagnostic. No session token, phone input, or global changes.
const { chromium } = require(process.env.AL1S_PLAYWRIGHT_MODULE || 'playwright');
const { resolve } = require('node:path');
(async () => {
  const browser = await chromium.launch({ headless: true, args: [
    '--no-proxy-server', '--host-resolver-rules=MAP RK3576-Tronlong.local 192.168.5.21',
    '--log-net-log=' + resolve(__dirname, '../../artifacts/browser/terminal-netlog.json'),
    ...(process.env.AL1S_DIAGNOSTIC_TLS12 === '1' ? ['--ssl-version-max=tls1.2'] : []),
  ] });
  try {
    console.log('Browser version:', browser.version());
    for (const host of ['rk3576-tronlong.local', '192.168.5.21', '127.0.0.1']) {
      const probe = await browser.newPage();
      try {
        const response = await probe.goto('https://' + host + ':8766/', { timeout: 10000 });
        console.log('Browser HTTPS', host, response.status());
      } catch (error) { console.log('Browser HTTPS', host, String(error.message).split('\n')[0]); }
      await probe.close();
    }
    const page = await browser.newPage();
    await page.goto('https://127.0.0.1:8443/login');
    const result = await page.evaluate(() => new Promise(resolve => {
      const socket = new WebSocket('wss://RK3576-Tronlong.local:8766/invalid');
      const timer = setTimeout(() => { socket.close(); resolve('timeout'); }, 10000);
      socket.onopen = () => { clearTimeout(timer); socket.close(); resolve('open'); };
      socket.onerror = () => { clearTimeout(timer); resolve('error'); };
    }));
    console.log('Browser WSS handshake:', result);
  } finally { await browser.close(); }
})().catch(error => { console.error(String(error.message).split('\n')[0]); process.exitCode = 1; });
