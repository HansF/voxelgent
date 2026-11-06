import { readFileSync } from 'node:fs';

import { normalizeFeed } from './gtfs.mjs';

const UPSTREAM = 'https://api.delijn.be/gtfs/v3/realtime?json=true&position=true&delay=true&canceled=true';
const geo = JSON.parse(readFileSync(new URL('../public/ghent/georeference.json', import.meta.url), 'utf8'));

async function readJson(response) {
  const limit = 32 * 1024 * 1024;
  if (Number(response.headers.get('content-length')) > limit) throw new Error('Feed too large');
  const reader = response.body.getReader(), chunks = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > limit) { await reader.cancel(); throw new Error('Feed too large'); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

export function createDeLijnService({ key = '', fetchImpl = fetch, now = Date.now, routes = {} } = {}) {
  let cache = null, lastGood = null, expires = 0, inFlight = null;
  const result = (snapshot, state, message) => ({ ...snapshot, state, message });
  const empty = () => ({ source: 'De Lijn', updatedAt: null, fetchedAt: null, vehicles: [] });
  const ageState = (value) => {
    const current = now();
    const vehicles = value.vehicles.filter((vehicle) => current - Date.parse(vehicle.observedAt) <= 180000);
    const stale = current - Date.parse(value.updatedAt) > 90000 || vehicles.some((vehicle) => current - Date.parse(vehicle.observedAt) > 90000);
    return { ...value, vehicles, state: value.state === 'live' && stale ? 'stale' : value.state };
  };
  async function refresh() {
    try {
      const response = await fetchImpl(UPSTREAM, { headers: { 'Ocp-Apim-Subscription-Key': key, Accept: 'application/json' }, signal: AbortSignal.timeout(15000), redirect: 'error' });
      if (!response.ok) {
        // Never forward upstream bodies or errors: they may contain request credentials.
        const reason = [401, 403].includes(response.status) ? 'De Lijn access is not authorized.' : 'De Lijn is temporarily unavailable.';
        throw new Error(reason);
      }
      const snapshot = normalizeFeed(await readJson(response), geo, now(), routes);
      if (lastGood && Date.parse(snapshot.updatedAt) < Date.parse(lastGood.updatedAt)) throw new Error('Feed timestamp regressed');
      lastGood = snapshot;
      cache = ageState(result(snapshot, 'live', null));
    } catch (error) {
      const message = error.message === 'De Lijn access is not authorized.' ? error.message : 'De Lijn is temporarily unavailable.';
      const recent = lastGood && now() - Date.parse(lastGood.updatedAt) <= 180000;
      cache = result(recent ? ageState(lastGood) : empty(), recent ? 'stale' : 'unavailable', message);
    }
    expires = now() + 30000;
    return ageState(cache);
  }
  return {
    async get() {
      if (!key.trim()) return result(empty(), 'unconfigured', 'Live transit is not connected.');
      if (cache && now() < expires) return ageState(cache);
      if (!inFlight) inFlight = refresh().finally(() => { inFlight = null; });
      return inFlight;
    },
  };
}

export function createDeLijnMiddleware(options = {}) {
  const service = createDeLijnService(options);
  return async (req, res, next) => {
    if (new URL(req.url, 'http://localhost').pathname !== '/api/delijn') { next(); return; }
    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    if (options.allowedOrigin) {
      res.setHeader('Access-Control-Allow-Origin', options.allowedOrigin);
      res.setHeader('Vary', 'Origin');
    }
    if (req.method !== 'GET') {
      res.statusCode = 405; res.setHeader('Allow', 'GET'); res.end(JSON.stringify({ state: 'unavailable', vehicles: [], message: 'Method not allowed.' })); return;
    }
    const payload = await service.get();
    res.statusCode = payload.state === 'unconfigured' ? 503 : payload.state === 'unavailable' ? 502 : 200;
    res.end(JSON.stringify(payload));
  };
}

export function loadRoutes(path) {
  return path ? JSON.parse(readFileSync(path, 'utf8')) : {};
}
