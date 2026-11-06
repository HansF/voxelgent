import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { test } from 'node:test';
import { normalizeFeed, projectPosition } from './gtfs.mjs';
import { createDeLijnMiddleware, createDeLijnService } from './delijn.mjs';

const geo = JSON.parse(readFileSync(new URL('../public/ghent/georeference.json', import.meta.url)));
const NOW = Date.UTC(2026, 10, 6, 12), SEC = NOW / 1000;
const copy = (value) => structuredClone(value);
const feed = () => ({ header: { gtfs_realtime_version: '2.0', timestamp: String(SEC) }, entity: [
  { id: 'position', vehicle: { vehicle: { id: '123', label: 'Vehicle 123' },
    trip: { trip_id: 'trip', route_id: 'route', start_date: '20261106' },
    position: { latitude: 51.0536, longitude: 3.7249, bearing: 90 }, timestamp: SEC, current_stop_sequence: 10 } },
  { id: 'update', trip_update: { trip: { trip_id: 'trip', start_date: '20261106' }, timestamp: SEC,
    stop_time_update: [{ stop_sequence: 1, arrival: { delay: 999 } }, { stop_sequence: 10, arrival: { delay: 120 } }] } },
] });
const jsonResponse = (body) => new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } });

test('WGS84 positions align with the real Belfry and exclude other cities and map edges', () => {
  const xy = projectPosition(3.7249, 51.0536, geo);
  assert.ok(Math.abs(xy.x - 856.10133182205) < 0.01);
  assert.ok(Math.abs(xy.y - 1088.71193478163) < 0.01);
  assert.equal(projectPosition(4.35, 50.85, geo), null);
  assert.equal(projectPosition(0, 0, geo), null);
  assert.equal(projectPosition(NaN, 51, geo), null);
  assert.equal(projectPosition(3.70, 51.035, geo), null);
});

test('joins positions and delays on the correct trip instance, uses optional route metadata', () => {
  const result = normalizeFeed(feed(), geo, NOW, { route: { name: 'T1', kind: 'tram' } });
  assert.equal(result.vehicles.length, 1);
  assert.deepEqual(result.vehicles[0], { id: '123', label: 'Vehicle 123', tripId: 'trip', routeId: 'route',
    routeName: 'T1', kind: 'tram', x: 856.1, y: 1088.7, bearing: 90,
    observedAt: new Date(NOW).toISOString(), delaySeconds: 120, canceled: false });
  const otherDay = feed(); otherDay.entity[1].trip_update.trip.start_date = '20261107';
  assert.equal(normalizeFeed(otherDay, geo, NOW).vehicles[0].delaySeconds, null);
  const noSequence = feed(); delete noSequence.entity[0].vehicle.current_stop_sequence;
  assert.equal(normalizeFeed(noSequence, geo, NOW).vehicles[0].delaySeconds, null);
});

test('accepts protobuf camelCase and C# PascalCase, and explicit zero delays', () => {
  const message = { Header: { Timestamp: SEC, Incrementality: 'FULL_DATASET' }, Entity: [
    { Id: 'v', Vehicle: { Vehicle: { Id: 'v1' }, Trip: { TripId: 'trip' },
      Position: { Longitude: 3.7249, Latitude: 51.0536 }, Timestamp: SEC } },
    { Id: 'u', TripUpdate: { Trip: { TripId: 'trip', ScheduleRelationship: 'CANCELED' }, Delay: 0 } },
  ] };
  const vehicle = normalizeFeed(message, geo, NOW).vehicles[0];
  assert.equal(vehicle.canceled, true); assert.equal(vehicle.delaySeconds, 0);
  assert.equal(vehicle.bearing, null); assert.equal(vehicle.routeName, null); assert.equal(vehicle.kind, 'vehicle');
  const camel = feed(); camel.entity[1].tripUpdate = camel.entity[1].trip_update; delete camel.entity[1].trip_update;
  assert.equal(normalizeFeed(camel, geo, NOW).vehicles[0].delaySeconds, 120);
});

test('rejects malformed and differential feeds; empty full feeds are valid', () => {
  assert.throws(() => normalizeFeed({}, geo, NOW));
  assert.throws(() => normalizeFeed({ header: { timestamp: SEC + 120 }, entity: [] }, geo, NOW));
  assert.throws(() => normalizeFeed({ header: { timestamp: SEC, incrementality: 1 }, entity: [] }, geo, NOW));
  assert.deepEqual(normalizeFeed({ header: { timestamp: SEC } }, geo, NOW).vehicles, []);
});

test('filters stale GPS fixes, missing coordinates, deleted entities and duplicate vehicle ids', () => {
  const input = feed();
  const original = input.entity[0];
  const old = copy(original); old.vehicle.timestamp = SEC - 300;
  const outside = copy(original); outside.vehicle.position.longitude = 4.35;
  const missing = copy(original); delete missing.vehicle.position.latitude;
  const duplicate = copy(original); duplicate.vehicle.timestamp = SEC - 10; duplicate.vehicle.position.longitude = 3.725;
  const deleted = copy(original); deleted.is_deleted = true;
  input.entity = [original, old, outside, missing, duplicate, deleted];
  const result = normalizeFeed(input, geo, NOW);
  assert.equal(result.vehicles.length, 1); assert.equal(result.discarded, 3);
  assert.equal(result.vehicles[0].x, 856.1);
});

test('stale trip updates cannot apply a cancellation or delay to a current position', () => {
  const input = feed(); input.entity[1].trip_update.timestamp = SEC - 100;
  input.entity[1].trip_update.trip.schedule_relationship = 3;
  const result = normalizeFeed(input, geo, NOW).vehicles[0];
  assert.equal(result.delaySeconds, null); assert.equal(result.canceled, false);
});

test('unconfigured proxy makes no upstream request', async () => {
  const service = createDeLijnService({ fetchImpl: () => { throw new Error('Should not fetch'); } });
  assert.equal((await service.get()).state, 'unconfigured');
});

test('authenticates only server-side, shares concurrent requests and caches for 30 seconds', async () => {
  let calls = 0, clock = NOW, release;
  const pending = new Promise((resolve) => { release = resolve; });
  const service = createDeLijnService({ key: 'test-secret', now: () => clock, fetchImpl: async (url, options) => {
    calls++; assert.equal(new URL(url).searchParams.get('position'), 'true');
    assert.equal(options.headers['Ocp-Apim-Subscription-Key'], 'test-secret');
    assert.ok(!url.includes('test-secret')); await pending; return jsonResponse(feed());
  } });
  const requests = [service.get(), service.get(), service.get()]; release();
  const values = await Promise.all(requests);
  assert.equal(calls, 1); assert.equal(values[0].state, 'live');
  assert.ok(!JSON.stringify(values).includes('test-secret'));
  clock += 29999; await service.get(); assert.equal(calls, 1);
  clock += 2; await service.get(); assert.equal(calls, 2);
});

test('failures retain recent positions as stale then expire them, and never leak errors or secrets', async () => {
  let clock = NOW, calls = 0;
  const service = createDeLijnService({ key: 'test-secret', now: () => clock, fetchImpl: async () => {
    if (calls++ === 0) return jsonResponse(feed());
    throw new Error('secret upstream URL test-secret');
  } });
  assert.equal((await service.get()).state, 'live');
  clock += 31000;
  const stale = await service.get();
  assert.equal(stale.state, 'stale'); assert.equal(stale.vehicles.length, 1);
  assert.ok(!JSON.stringify(stale).includes('test-secret'));
  clock += 180000;
  const expired = await service.get();
  assert.equal(expired.state, 'unavailable'); assert.deepEqual(expired.vehicles, []);
});

test('a cached snapshot becomes stale without another upstream request', async () => {
  let clock = NOW;
  const input = feed(); input.entity[0].vehicle.timestamp = SEC - 85;
  const service = createDeLijnService({ key: 'test', now: () => clock, fetchImpl: async () => jsonResponse(input) });
  assert.equal((await service.get()).state, 'live');
  clock += 10000;
  assert.equal((await service.get()).state, 'stale');
});

test('unauthorized and oversized responses fail safely; regressed feeds do not replace newer positions', async () => {
  const unauthorized = createDeLijnService({ key: 'test', now: () => NOW, fetchImpl: async () => new Response('private', { status: 401 }) });
  assert.equal((await unauthorized.get()).message, 'De Lijn access is not authorized.');
  const oversized = createDeLijnService({ key: 'test', now: () => NOW, fetchImpl: async () => new Response('{}', { headers: { 'content-length': 999999999 } }) });
  assert.equal((await oversized.get()).state, 'unavailable');
  let clock = NOW, calls = 0;
  const regressed = createDeLijnService({ key: 'test', now: () => clock, fetchImpl: async () => {
    const input = feed(); if (calls++) input.header.timestamp = SEC - 20;
    return jsonResponse(input);
  } });
  const first = await regressed.get(); clock += 31000;
  const second = await regressed.get();
  assert.equal(second.updatedAt, first.updatedAt); assert.equal(second.state, 'stale');
});

test('HTTP middleware allows only GET, returns sanitized JSON, and ignores client query filters', async (t) => {
  const middleware = createDeLijnMiddleware({ key: 'test', now: () => NOW, fetchImpl: async () => jsonResponse(feed()) });
  const server = createServer((req, res) => void middleware(req, res, () => { res.writeHead(404).end(); }));
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const endpoint = `http://127.0.0.1:${server.address().port}/api/delijn`;
  const response = await fetch(endpoint + '?url=https://attacker.example&key=ignored');
  assert.equal(response.status, 200); assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.equal((await response.json()).vehicles.length, 1);
  assert.equal((await fetch(endpoint, { method: 'POST' })).status, 405);
});
