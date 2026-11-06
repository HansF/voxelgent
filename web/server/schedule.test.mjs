import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { gunzipSync } from 'node:zlib';
import { test } from 'node:test';
import { SchedulePlayer, distanceAtTime, localServiceDate, pointAtDistance, serviceRuns, serviceStart } from '../src/schedule.ts';

const service = { start: '20260101', end: '20261231', weekdays: [1, 1, 1, 1, 1, 0, 0], added: [], removed: [] };
function fixture() {
  return { version: 1, publisher: 'test', sourcePage: 'https://example.com', generatedAt: '2026-11-06', feedVersion: 'test',
    validFrom: '20261101', validUntil: '20261130', timezone: 'Europe/Brussels', W: 2000, D: 2000, maxTime: 90000,
    routes: { line: { name: 'T1', kind: 'tram' } }, services: { weekdays: structuredClone(service) },
    shapes: { route: [[[100, 100, 0], [100, 200, 100], [200, 200, 200]]] },
    patterns: { stops: { shape: 'route', distances: [0, 200] } },
    trips: [{ id: 'trip', route: 'line', service: 'weekdays', pattern: 'stops', headsign: 'Destination', times: [[43200, 43210], [43230, 43240]] }] };
}

test('calendars honor weekday service and added/removed date exceptions', () => {
  assert.equal(serviceRuns(service, '20261106'), true); // Friday
  assert.equal(serviceRuns(service, '20261107'), false); // Saturday
  assert.equal(serviceRuns({ ...service, added: ['20261107'] }, '20261107'), true);
  assert.equal(serviceRuns({ ...service, removed: ['20261106'] }, '20261106'), false);
  assert.equal(serviceRuns(service, '20270101'), false);
  assert.equal(serviceRuns({ start: null, end: null, weekdays: [], added: ['20261106'], removed: [] }, '20261106'), true);
});

test('local service dates and GTFS noon anchors handle Brussels daylight saving time', () => {
  assert.equal(localServiceDate(Date.parse('2026-11-05T23:30:00Z')), '20261106');
  assert.equal(serviceStart('20261106'), Date.parse('2026-11-05T23:00:00Z'));
  assert.equal(serviceStart('20260706'), Date.parse('2026-07-05T22:00:00Z'));
  // On both clock-change days local noon is exactly twelve GTFS hours after the anchor.
  assert.equal(serviceStart('20260329') + 43200000, Date.parse('2026-03-29T10:00:00Z'));
  assert.equal(serviceStart('20261025') + 43200000, Date.parse('2026-10-25T11:00:00Z'));
});

test('interpolation holds during dwell and follows shape distance between timed stops', () => {
  const times = [[100, 110], [130, 140]], distances = [0, 200];
  assert.equal(distanceAtTime(times, distances, 99), null);
  assert.equal(distanceAtTime(times, distances, 105), 0);
  assert.equal(distanceAtTime(times, distances, 120), 100);
  assert.equal(distanceAtTime(times, distances, 135), 200);
  assert.equal(distanceAtTime(times, distances, 141), null);
  const point = pointAtDistance(fixture().shapes.route, 150);
  assert.deepEqual(point, { x: 150, y: 200, bearing: 90 });
  assert.equal(pointAtDistance([[[0, 0, 0], [1, 1, 10]], [[50, 50, 100], [60, 50, 110]]], 50), null);
});

test('scheduled positions animate, respect the map boundary and never claim GPS or punctuality', () => {
  const player = new SchedulePlayer(fixture());
  const start = serviceStart('20261106');
  const first = player.positions(start + 43215000)[0], second = player.positions(start + 43225000)[0];
  assert.equal(first.id, 'scheduled:20261106:trip'); assert.equal(first.routeName, 'T1');
  assert.equal(first.x, 100); assert.equal(first.y, 150); assert.equal(second.x, 150); assert.equal(second.y, 200);
  assert.equal('observedAt' in first, false); assert.equal('delaySeconds' in first, false); assert.equal('canceled' in first, false);
  assert.deepEqual(player.positions(start + 43241000), []);
  const outside = fixture(); outside.shapes.route = [[[2100, 100, 0], [2300, 100, 200]]];
  assert.deepEqual(new SchedulePlayer(outside).positions(start + 43215000), []);
});

test('after-midnight trips keep the prior service date and its calendar exceptions', () => {
  const input = fixture(); input.trips[0].times = [[88200, 88200], [90000, 90000]];
  const now = Date.parse('2026-11-06T23:40:00Z'); // 00:40 Saturday, Friday service 24:40
  assert.equal(localServiceDate(now), '20261107');
  const positions = new SchedulePlayer(input).positions(now);
  assert.equal(positions.length, 1); assert.equal(positions[0].serviceDate, '20261106');
  input.services.weekdays.removed = ['20261106'];
  assert.deepEqual(new SchedulePlayer(input).positions(now), []);
});

test('spring-DST service can begin at its noon-minus-twelve anchor before local midnight', () => {
  const input = fixture(); input.validFrom = '20260301'; input.validUntil = '20260331';
  input.services.weekdays = { start: null, end: null, weekdays: [], added: ['20260329'], removed: [] };
  input.trips[0].times = [[0, 0], [1800, 1800]];
  const now = Date.parse('2026-03-28T22:15:00Z');
  assert.equal(localServiceDate(now), '20260328');
  assert.equal(new SchedulePlayer(input).positions(now)[0].serviceDate, '20260329');
});

test('expired or not-yet-valid feeds do not replay trips on a made-up date', () => {
  const player = new SchedulePlayer(fixture());
  assert.deepEqual(player.positions(Date.parse('2026-10-30T11:00:15Z')), []);
  assert.deepEqual(player.positions(Date.parse('2026-12-01T11:00:15Z')), []);
  assert.equal(player.expired(Date.parse('2027-01-01T11:00:00Z')), true);
});

test('the actual public extract has usable in-bounds service and a compact size', () => {
  const compressed = readFileSync(new URL('../public/ghent/delijn-schedule.json.gz', import.meta.url));
  const input = JSON.parse(gunzipSync(compressed));
  assert.ok(compressed.byteLength < 3 * 1024 * 1024);
  assert.equal(input.timezone, 'Europe/Brussels');
  assert.ok(input.trips.length > 0); assert.ok(Object.values(input.routes).some((route) => route.kind === 'tram'));
  const firstDay = serviceStart(input.validFrom);
  const player = new SchedulePlayer(input);
  let found = 0;
  for (let hour = 8; hour <= 20; hour++) {
    const positions = player.positions(firstDay + hour * 3600000);
    found += positions.length;
    assert.ok(positions.every((point) => point.x >= 0 && point.x < input.W && point.y >= 0 && point.y < input.D));
  }
  assert.ok(found > 0, 'No scheduled service found on the feed start date');
});
