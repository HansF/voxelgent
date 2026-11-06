// Pure timetable playback. No GPS, actual vehicle assignments or punctuality claims.
export type ShapePoint = [number, number, number]; // grid x, grid y, distance along full shape (m)
export interface Service {
  start: string | null; end: string | null; weekdays: number[]; added: string[]; removed: string[];
}
export interface ScheduledTrip {
  id: string; route: string; service: string; pattern: string; headsign: string; times: [number, number][];
}
export interface ScheduleData {
  version: number; publisher: string; sourcePage: string; generatedAt: string; feedVersion: string;
  validFrom: string | null; validUntil: string | null; timezone: string; W: number; D: number; maxTime: number;
  routes: Record<string, { name: string; kind: 'bus' | 'tram' | 'vehicle' }>;
  services: Record<string, Service>;
  shapes: Record<string, ShapePoint[][]>;
  patterns: Record<string, { shape: string; distances: number[] }>;
  trips: ScheduledTrip[];
}
export interface ScheduledPosition {
  id: string; tripId: string; routeId: string; routeName: string; kind: 'bus' | 'tram' | 'vehicle';
  headsign: string; x: number; y: number; bearing: number; serviceDate: string;
}

const formatter = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/Brussels', hourCycle: 'h23',
  year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
function localParts(now: number) {
  return Object.fromEntries(formatter.formatToParts(new Date(now)).filter((part) => part.type !== 'literal').map((part) => [part.type, part.value]));
}
export function localServiceDate(now: number): string {
  const parts = localParts(now);
  return `${parts.year}${parts.month}${parts.day}`;
}
function dateUtc(date: string) { return Date.UTC(+date.slice(0, 4), +date.slice(4, 6) - 1, +date.slice(6, 8)); }
function shiftedDate(date: string, days: number) { return new Date(dateUtc(date) + days * 86400000).toISOString().slice(0, 10).replaceAll('-', ''); }

// GTFS times count seconds from local noon minus twelve hours, including DST days.
export function serviceStart(date: string): number {
  const noonUtc = dateUtc(date) + 12 * 3600000;
  const parts = localParts(noonUtc);
  const asUtc = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second);
  return noonUtc - (asUtc - noonUtc) - 12 * 3600000;
}

export function serviceRuns(service: Service | undefined, date: string): boolean {
  if (!service || service.removed.includes(date)) return false;
  if (service.added.includes(date)) return true;
  const weekday = (new Date(dateUtc(date)).getUTCDay() + 6) % 7;
  return !!(service.start && service.end && date >= service.start && date <= service.end && service.weekdays[weekday]);
}

export function distanceAtTime(times: [number, number][], distances: number[], seconds: number): number | null {
  if (times.length < 2 || times.length !== distances.length || seconds < times[0][0] || seconds > times[times.length - 1][1]) return null;
  for (let i = 0; i < times.length; i++) {
    const [arrival, departure] = times[i];
    if (seconds >= arrival && seconds <= departure) return distances[i];
    if (i + 1 < times.length && seconds > departure && seconds < times[i + 1][0]) {
      const fraction = (seconds - departure) / (times[i + 1][0] - departure);
      return distances[i] + fraction * (distances[i + 1] - distances[i]);
    }
  }
  return null;
}

export function pointAtDistance(chunks: ShapePoint[][], distance: number): { x: number; y: number; bearing: number } | null {
  const chunk = chunks.find((points) => points.length >= 2 && distance >= points[0][2] && distance <= points[points.length - 1][2]);
  if (!chunk) return null; // Between separate map visits: never invent a connecting line.
  let lo = 0, hi = chunk.length - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (chunk[mid][2] <= distance) lo = mid; else hi = mid; }
  const a = chunk[lo], b = chunk[hi], span = b[2] - a[2];
  const fraction = span > 0 ? Math.min(1, Math.max(0, (distance - a[2]) / span)) : 0;
  return { x: a[0] + fraction * (b[0] - a[0]), y: a[1] + fraction * (b[1] - a[1]),
    bearing: (Math.atan2(b[0] - a[0], -(b[1] - a[1])) * 180 / Math.PI + 360) % 360 };
}

export class SchedulePlayer {
  readonly data: ScheduleData;
  private bucket = -1;
  private candidates: { trip: ScheduledTrip; date: string; start: number }[] = [];
  constructor(data: ScheduleData) {
    if (data.version !== 1 || data.timezone !== 'Europe/Brussels' || !Array.isArray(data.trips)) throw new Error('Unsupported timetable data');
    this.data = data;
  }

  positions(now: number): ScheduledPosition[] {
    const { data } = this;
    const bucket = Math.floor(now / 30000);
    if (bucket !== this.bucket) {
      this.bucket = bucket; this.candidates = [];
      const today = localServiceDate(now);
      // A spring-DST service day's noon-minus-12h anchor can begin before local midnight.
      for (let days = -1; days <= Math.min(4, Math.ceil(data.maxTime / 86400)); days++) {
        const date = shiftedDate(today, -days);
        if ((data.validFrom && date < data.validFrom) || (data.validUntil && date > data.validUntil)) continue;
        const start = serviceStart(date), seconds = (now - start) / 1000;
        const active = new Set(Object.entries(data.services).filter(([, service]) => serviceRuns(service, date)).map(([id]) => id));
        for (const trip of data.trips) {
          if (!active.has(trip.service) || !trip.times.length) continue;
          if (trip.times[0][0] <= seconds + 31 && trip.times[trip.times.length - 1][1] >= seconds - 31) this.candidates.push({ trip, date, start });
        }
      }
    }
    const result: ScheduledPosition[] = [];
    for (const { trip, date, start } of this.candidates) {
      const pattern = data.patterns[trip.pattern];
      if (!pattern) continue;
      const distance = distanceAtTime(trip.times, pattern.distances, (now - start) / 1000);
      if (distance === null) continue;
      const position = pointAtDistance(data.shapes[pattern.shape] || [], distance);
      if (!position || position.x < 0 || position.y < 0 || position.x >= data.W || position.y >= data.D) continue;
      const route = data.routes[trip.route];
      result.push({ id: `scheduled:${date}:${trip.id}`, tripId: trip.id, routeId: trip.route,
        routeName: route?.name || trip.route, kind: route?.kind || 'vehicle', headsign: trip.headsign,
        serviceDate: date, ...position });
    }
    return result;
  }

  expired(now: number): boolean {
    if (!this.data.validUntil) return false;
    return now > serviceStart(this.data.validUntil) + this.data.maxTime * 1000;
  }
}
