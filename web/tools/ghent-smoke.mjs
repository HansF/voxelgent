// Usage: node tools/ghent-smoke.mjs [screenshot-path]
import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
try {
  const page = await browser.newPage({ viewport: { width: 1000, height: 700 } });
  const errors = [], failed = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('response', (response) => { if (response.status() >= 400 && response.url().includes('localhost') && !response.url().includes('/api/delijn')) failed.push(response.url()); });
  await page.goto(process.env.URL || 'http://localhost:5173/');
  await page.waitForFunction(() => window.__ready === true, null, { timeout: 120000 });
  await page.waitForFunction(() => !document.body.classList.contains('loading'), null, { timeout: 120000 });
  assert.match(await page.title(), /Ghent/);
  assert.match(await page.locator('#credits').innerText(), /Groendienst Gent/);
  await page.screenshot({ path: process.argv[2] || '/tmp/ghent-overview.png', timeout: 120000 });
  const before = await page.evaluate(() => {
    const { camera, setHour, setWeather } = window.__three;
    setHour(21); setWeather('rain', true);
    return { position: camera.position.toArray(), rain: window.__three.daylight.weather.rain };
  });
  assert.equal(before.rain, 1);
  await page.evaluate(() => { window.__three.setHour(12); window.__three.setWeather('clear', true); });
  const search = page.locator('input[type="search"], input[placeholder]').first();
  await search.fill('Gravensteen');
  await page.locator('.search ol li').filter({ hasText: 'Gravensteen' }).first().waitFor();
  await search.press('Escape');
  await page.evaluate(() => {
    const { camera, controls } = window.__three;
    camera.position.x += 30;
    controls.target.x += 30;
  });
  assert.notDeepEqual(await page.evaluate(() => window.__three.camera.position.toArray()), before.position);
  assert.deepEqual(errors, [], `Browser errors: ${errors}`);
  assert.deepEqual(failed, [], `Failed map assets: ${failed}`);
  console.log('Ghent loaded; weather, time, search, camera and all requested assets passed.');
} finally {
  await browser.close();
}
