// Uses the actual bundled public timetable, with the live API unavailable.
import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
try {
  const page = await browser.newPage({ viewport: { width: 1000, height: 700 } });
  const errors = [];
  page.on('pageerror', (error) => { errors.push(error.message); console.log('Page error:', error.message); });
  page.on('requestfailed', (request) => console.log('Request failed:', request.url(), request.failure()?.errorText));
  await page.route('**/api/delijn', (route) => route.fulfill({ status: 503,
    json: { state: 'unconfigured', source: 'De Lijn', updatedAt: null, fetchedAt: null, vehicles: [] } }));
  await page.goto(`${process.env.URL || 'http://localhost:5173/'}?hour=12&tilt=0`);
  await page.waitForFunction(() => document.querySelector('.transit')?.dataset.state === 'scheduled'
    || document.querySelector('#loading-text')?.textContent.startsWith('Could not load'), null, { timeout: 120000, polling: 1000 });
  assert.equal(await page.locator('.transit').count(), 1, `Map failed to boot: ${await page.locator('#loading-text').textContent()}`);
  console.log('Timetable fallback loaded.');
  await page.waitForFunction(() => !document.body.classList.contains('loading'), null, { timeout: 120000, polling: 1000 });
  console.log('City loaded.');
  assert.match(await page.locator('.transit').innerText(), /scheduled estimates/);
  assert.match(await page.locator('.transit').innerText(), /not live GPS/);
  const positions = await page.evaluate(() => window.__three.scene.getObjectByName('De Lijn transit vehicles').children.map((group) => ({ id: group.userData.vehicleId, x: group.position.x, z: group.position.z })));
  assert.ok(positions.length > 0, 'No scheduled vehicles in the current timetable');
  await page.waitForFunction((previous) => {
    const groups = window.__three.scene.getObjectByName('De Lijn transit vehicles').children;
    return groups.some((group) => {
      const old = previous.find((entry) => entry.id === group.userData.vehicleId);
      return old && Math.hypot(old.x - group.position.x, old.z - group.position.z) > 2;
    });
  }, positions, { timeout: 30000, polling: 1000 });
  const pick = await page.evaluate(() => {
    const { scene, camera } = window.__three;
    for (const group of scene.getObjectByName('De Lijn transit vehicles').children) {
      const sprite = group.children[1], v = camera.position.clone();
      sprite.getWorldPosition(v); v.project(camera);
      const x = (v.x + 1) * innerWidth / 2, y = (1 - v.y) * innerHeight / 2;
      if (v.z < 1 && x > 370 && x < innerWidth - 20 && y > 140 && y < innerHeight - 100) return { x, y };
    }
    return null;
  });
  assert.ok(pick, 'No visible transit badge to select');
  await page.mouse.click(pick.x, pick.y);
  await page.waitForFunction(() => document.querySelector('.transit p:not([aria-live])')?.textContent.includes('Scheduled estimate'), null, { polling: 1000 });
  const details = await page.locator('.transit p:not([aria-live])').innerText();
  assert.match(details, /Actual delays and cancellations unknown/);
  assert.doesNotMatch(details, /On time|GPS \d/);
  await page.screenshot({ path: process.argv[2] || '../ghent-scheduled-preview.png', timeout: 120000 });
  const toggle = page.locator('.transit button');
  await toggle.click();
  assert.equal(await toggle.getAttribute('aria-pressed'), 'false');
  assert.deepEqual(errors, []);
  console.log(`Scheduled transit passed: ${positions.length} estimated trips, motion along routes, selectable badges, explicit estimate labels, unknown punctuality and pause. Actual public timetable, no API key.`);
} finally { await browser.close(); }
