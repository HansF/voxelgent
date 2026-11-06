// UI integration test with explicitly mocked API responses; never contacts De Lijn.
import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
try {
  const page = await browser.newPage({ viewport: { width: 900, height: 650 } });
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  let mode = 'live', requests = 0;
  // Exercise unavailable states when the optional timetable asset is also missing.
  await page.route('**/ghent/delijn-schedule.json.gz', (route) => route.fulfill({ status: 404, body: 'missing' }));
  await page.route('**/api/delijn', (route) => {
    requests++;
    const updatedAt = new Date(Date.now() - (mode === 'stale' ? 110000 : 0)).toISOString();
    return route.fulfill({ status: mode === 'unconfigured' ? 503 : mode === 'unavailable' ? 502 : 200,
      json: { state: mode === 'empty' ? 'live' : mode, updatedAt, fetchedAt: new Date().toISOString(),
        vehicles: ['empty', 'unconfigured', 'unavailable'].includes(mode) ? [] : [
          { id: 'test-only', label: '<test vehicle>', routeId: 'test-route', routeName: 'TEST', kind: 'tram', tripId: 'test-trip',
            x: 660, y: 1060, bearing: 45, observedAt: updatedAt, delaySeconds: 120, canceled: false },
        ] } });
  });
  await page.goto(`${process.env.URL || 'http://localhost:5173/'}?hour=12&tilt=0`);
  await page.waitForFunction(() => window.__ready === true, null, { timeout: 120000, polling: 1000 });
  const panel = page.locator('.transit'), toggle = panel.locator('button');
  await page.waitForFunction(() => document.querySelector('.transit')?.dataset.state === 'live', null, { polling: 1000 });
  assert.match(await panel.innerText(), /1 vehicle in this area/);
  assert.equal(await page.evaluate(() => window.__three.scene.getObjectByName('De Lijn transit vehicles').children.length), 1);
  // Stop the expensive software renderer once the scene and layer have initialized.
  await page.evaluate(() => window.__three.renderer.setAnimationLoop(null));
  await toggle.click();
  assert.equal(await toggle.getAttribute('aria-pressed'), 'false');
  assert.equal(await page.evaluate(() => window.__three.scene.getObjectByName('De Lijn transit vehicles').visible), false);
  const pausedRequests = requests;
  for (const next of ['stale', 'empty', 'unconfigured', 'unavailable', 'live']) {
    mode = next;
    await toggle.click();
    const state = next === 'empty' ? 'live' : next;
    await page.waitForFunction((value) => document.querySelector('.transit')?.dataset.state === value, state, { polling: 100 });
    if (next === 'stale') assert.match(await panel.innerText(), /Last known positions/);
    if (next === 'empty') assert.match(await panel.innerText(), /No recent vehicles/);
    if (next === 'unconfigured') assert.match(await panel.innerText(), /not connected/);
    if (next === 'unavailable') assert.match(await panel.innerText(), /unavailable/);
    if (next !== 'live') await toggle.click();
  }
  assert.ok(requests > pausedRequests);
  assert.equal(await page.evaluate(() => window.__three.scene.getObjectByName('De Lijn transit vehicles').children.length), 1);
  assert.deepEqual(errors, []);
  console.log('Transit UI passed: vehicle rendering, toggle, stale data, empty feed, missing key, outage and recovery. All API data was mocked.');
} finally { await browser.close(); }
