// GTFS-RT JSON accepts the protobuf snake_case, lowerCamelCase and C# PascalCase spellings.
const field = (object, name) => {
  const camel = name.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase());
  return object?.[name] ?? object?.[camel] ?? object?.[camel[0].toUpperCase() + camel.slice(1)];
};
const number = (value) => (typeof value !== 'number' && typeof value !== 'string') || (typeof value === 'string' && !value.trim())
  ? null : Number.isFinite(Number(value)) ? Number(value) : null;
const text = (value) => typeof value === 'string' || typeof value === 'number' ? String(value).slice(0, 160) : null;
const canceled = (trip) => ['CANCELED', 'CANCELLED', 3, '3'].includes(field(trip, 'schedule_relationship'));

export function projectPosition(lon, lat, geo) {
  if (!Number.isFinite(lon) || !Number.isFinite(lat)) return null;
  const [west, south, east, north] = geo.bounds;
  if (lon < west || lon > east || lat < south || lat > north) return null;
  const u = (lon - geo.anchor[0]) * geo.scale, v = (lat - geo.anchor[1]) * geo.scale;
  const basis = [1, u, v, u * u, u * v, v * v];
  const x = basis.reduce((sum, value, i) => sum + value * geo.x[i], 0);
  const y = basis.reduce((sum, value, i) => sum + value * geo.y[i], 0);
  return x >= 0 && y >= 0 && x < geo.W && y < geo.D ? { x, y } : null;
}

function tripKey(trip) {
  const id = text(field(trip, 'trip_id'));
  return id ? JSON.stringify([id, text(field(trip, 'start_date')), text(field(trip, 'start_time'))]) : null;
}

function reportedDelay(update, sequence) {
  const delay = number(field(update, 'delay'));
  if (delay !== null) return delay;
  // Without a known current stop, don't guess which stop's delay applies.
  if (sequence === null) return null;
  const stops = field(update, 'stop_time_update');
  if (!Array.isArray(stops)) return null;
  const next = stops.filter((stop) => number(field(stop, 'stop_sequence')) !== null && number(field(stop, 'stop_sequence')) >= sequence)
    .sort((a, b) => number(field(a, 'stop_sequence')) - number(field(b, 'stop_sequence')))[0];
  return number(field(field(next, 'arrival'), 'delay')) ?? number(field(field(next, 'departure'), 'delay'));
}

export function normalizeFeed(feed, geo, now = Date.now(), routes = {}) {
  const header = field(feed, 'header'), entities = field(feed, 'entity') ?? [];
  const timestamp = number(field(header, 'timestamp'));
  if (!header || !Array.isArray(entities) || timestamp === null || timestamp <= 0 || timestamp * 1000 > now + 60000) throw new Error('Invalid GTFS feed');
  const incrementality = field(header, 'incrementality');
  if (incrementality !== undefined && ![0, '0', 'FULL_DATASET'].includes(incrementality)) throw new Error('Differential GTFS feeds are not supported');
  const updates = new Map(), updatesById = new Map();
  for (const entity of entities) {
    if (field(entity, 'is_deleted')) continue;
    const update = field(entity, 'trip_update'), trip = field(update, 'trip');
    const key = tripKey(trip), id = text(field(trip, 'trip_id'));
    if (key) updates.set(key, update);
    if (id) { const entries = updatesById.get(id) ?? []; entries.push(update); updatesById.set(id, entries); }
  }
  const vehicles = new Map();
  let discarded = 0;
  for (const entity of entities) {
    if (field(entity, 'is_deleted')) continue;
    const vehicle = field(entity, 'vehicle'), position = field(vehicle, 'position');
    if (!position) continue;
    const lat = number(field(position, 'latitude')), lon = number(field(position, 'longitude'));
    const xy = lat === null || lon === null ? null : projectPosition(lon, lat, geo);
    const observed = number(field(vehicle, 'timestamp')) ?? timestamp;
    if (!xy || observed <= 0 || observed * 1000 > now + 60000 || now - observed * 1000 > 180000) { discarded++; continue; }
    const descriptor = field(vehicle, 'vehicle'), trip = field(vehicle, 'trip');
    const id = text(field(descriptor, 'id')) ?? text(field(entity, 'id'));
    if (!id) { discarded++; continue; }
    const tripId = text(field(trip, 'trip_id')), routeId = text(field(trip, 'route_id'));
    const sameTrip = updatesById.get(tripId) ?? [];
    // Match exact service instance first. Only use an id-only update when both sides omit instance fields.
    const update = updates.get(tripKey(trip)) ?? (sameTrip.length === 1 && !field(trip, 'start_date') && !field(trip, 'start_time')
      && !field(field(sameTrip[0], 'trip'), 'start_date') && !field(field(sameTrip[0], 'trip'), 'start_time') ? sameTrip[0] : null);
    const updateTime = number(field(update, 'timestamp')) ?? timestamp;
    const currentUpdate = updateTime * 1000 <= now + 60000 && now - updateTime * 1000 <= 90000 ? update : null;
    const bearing = number(field(position, 'bearing'));
    const route = routeId ? routes[routeId] : null;
    const record = { id, label: text(field(descriptor, 'label')), tripId, routeId,
      routeName: text(route?.name), kind: route?.kind === 'tram' || route?.kind === 'bus' ? route.kind : 'vehicle',
      x: Math.round(xy.x * 10) / 10, y: Math.round(xy.y * 10) / 10,
      bearing: bearing !== null && bearing >= 0 && bearing < 360 ? bearing : null,
      observedAt: new Date(observed * 1000).toISOString(),
      delaySeconds: reportedDelay(currentUpdate, number(field(vehicle, 'current_stop_sequence'))),
      canceled: canceled(trip) || canceled(field(currentUpdate, 'trip')) };
    const previous = vehicles.get(id);
    if (!previous || previous.observedAt < record.observedAt) vehicles.set(id, record);
  }
  return { source: 'De Lijn', updatedAt: new Date(timestamp * 1000).toISOString(),
    fetchedAt: new Date(now).toISOString(), vehicles: [...vehicles.values()].slice(0, 1024), discarded };
}
