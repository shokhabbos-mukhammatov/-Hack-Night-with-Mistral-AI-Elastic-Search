// Browser end-to-end + accessibility check (Playwright + axe-core).
//   node tests/e2e/a11y_and_flows.js <out_dir> [base_url]
// Fails (exit 1) on serious/critical axe violations or page errors.
const { chromium } = require('playwright');
(async () => {
  const out = process.argv[2] || '.';
  const BASE = (process.argv[3] || 'http://localhost:8000').replace(/\/$/, '');
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2500);
  await page.fill('#q', '17th and Folsom');
  await page.click('#search button[type=submit]');
  await page.waitForSelector('#brief .level', { timeout: 20000 });
  await page.waitForTimeout(2500);
  await page.screenshot({ path: out + '/briefing.png' });
  console.log('LEVEL', await page.textContent('#brief .level'));
  // axe accessibility audit
  await page.addScriptTag({ url: 'https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js' }).catch(e => errors.push('axe load: ' + e.message));
  const axe = await page.evaluate(async () => window.axe ? (await window.axe.run(document, { resultTypes: ['violations'] })).violations.map(v => ({ id: v.id, impact: v.impact, n: v.nodes.length, help: v.help, target: v.nodes[0].target })) : 'axe not loaded');
  console.log('AXE', JSON.stringify(axe));
  // replay tab
  await page.click('#tab-replay');
  await page.waitForTimeout(3000);
  await page.fill('#rslider', '40');
  await page.dispatchEvent('#rslider', 'input');
  await page.waitForTimeout(1500);
  console.log('CLOCK', await page.textContent('#rclock'), '| STATS', (await page.textContent('#rstats')).slice(0, 200));
  console.log('PHONE', (await page.textContent('#rmsgs')).slice(0, 200));
  await page.screenshot({ path: out + '/replay.png' });
  // mobile
  const m = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 1 });
  const mp = await m.newPage();
  await mp.goto(BASE + '/?q=Cesar%20Chavez%20and%20Mission', { waitUntil: 'domcontentloaded' });
  await mp.waitForSelector('#brief .level', { timeout: 20000 });
  await mp.waitForTimeout(1500);
  await mp.screenshot({ path: out + '/mobile.png', fullPage: false });
  console.log('ERRORS', JSON.stringify(errors.slice(0, 10)));
  await browser.close();
  const bad = Array.isArray(axe) ? axe.filter(v => ['serious', 'critical'].includes(v.impact)) : [];
  if (bad.length || errors.length) { console.error('E2E FAILED'); process.exit(1); }
  console.log('E2E OK');
})().catch(e => { console.error('FAIL', e.message); process.exit(1); });
