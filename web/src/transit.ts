import * as THREE from 'three';
import type { World } from './world';
import { fetchGzip } from './util';
import { localServiceDate, SchedulePlayer, type ScheduleData } from './schedule';

type TransitState = 'live' | 'stale' | 'unavailable' | 'unconfigured' | 'scheduled';
interface Vehicle {
  id: string; label: string | null; tripId: string | null; routeId: string | null;
  routeName: string | null; kind: 'bus' | 'tram' | 'vehicle'; x: number; y: number;
  bearing: number | null; observedAt: string | null; delaySeconds: number | null; canceled: boolean;
  estimated?: boolean; estimatedAt?: string; headsign?: string;
}
interface Snapshot {
  state: TransitState; updatedAt: string | null; fetchedAt: string | null; vehicles: Vehicle[]; message?: string | null;
}
interface Marker { group: THREE.Group; body: THREE.Mesh; sprite: THREE.Sprite; vehicle: Vehicle }

const time = (value: string) => new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const age = (value: string) => Date.now() - Date.parse(value);
const positionTime = (vehicle: Vehicle) => vehicle.estimated ? vehicle.estimatedAt || '' : vehicle.observedAt || '';
const scheduleClock = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/Brussels', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });

// Both transit modes use wall-clock time, independently of the scene's date/hour controls.
export class TransitLayer {
  private readonly group = new THREE.Group();
  private readonly markers = new Map<string, Marker>();
  private readonly panel = document.createElement('section');
  private readonly toggle = document.createElement('button');
  private readonly status = document.createElement('p');
  private readonly detail = document.createElement('p');
  private readonly source = document.createElement('a');
  private readonly geometry = new THREE.BoxGeometry(2.5, 3.2, 1).translate(0, 1.6, 0);
  private readonly raycaster = new THREE.Raycaster();
  private readonly pointer = new THREE.Vector2();
  private readonly projected = new THREE.Vector3();
  private request: AbortController | null = null;
  private timer = 0;
  private enabled = true;
  private snapshot: Snapshot | null = null;
  private selected: string | null = null;
  private pressed: [number, number] | null = null;
  private lastStatus = 0;
  private schedule: SchedulePlayer | null = null;
  private loadingSchedule: Promise<SchedulePlayer> | null = null;
  private lastScheduleUpdate = 0;
  private readonly endpoint = import.meta.env.VITE_DELIJN_PROXY_URL || new URL('api/delijn', document.baseURI).href;

  constructor(private readonly scene: THREE.Scene, private readonly world: World, private readonly canvas: HTMLCanvasElement) {
    this.group.name = 'De Lijn transit vehicles';
    scene.add(this.group);
    this.panel.className = 'transit';
    this.panel.setAttribute('aria-label', 'De Lijn transit');
    this.toggle.type = 'button';
    this.toggle.textContent = 'De Lijn · transit';
    this.toggle.setAttribute('aria-pressed', 'true');
    this.status.setAttribute('aria-live', 'polite');
    this.status.textContent = 'Connecting…';
    this.detail.hidden = true;
    this.source.href = 'https://portal.delijn.be/';
    this.source.textContent = 'Source: De Lijn';
    this.source.target = '_blank'; this.source.rel = 'noopener';
    this.panel.append(this.toggle, this.status, this.detail, this.source);
    document.body.append(this.panel);
    this.toggle.addEventListener('click', this.onToggle);
    document.addEventListener('visibilitychange', this.onVisibility);
    canvas.addEventListener('pointerdown', this.onPointerDown);
    canvas.addEventListener('pointerup', this.onPointerUp);
    window.addEventListener('pagehide', this.onPageHide);
    void this.poll();
  }

  private onToggle = () => {
    this.enabled = !this.enabled;
    this.toggle.setAttribute('aria-pressed', String(this.enabled));
    this.toggle.textContent = `De Lijn · ${this.enabled ? 'transit' : 'hidden'}`;
    clearTimeout(this.timer); this.request?.abort();
    this.group.visible = this.enabled;
    if (this.enabled) void this.poll();
    else { this.status.textContent = 'Transit animation paused'; this.detail.hidden = true; }
  };

  private onVisibility = () => {
    clearTimeout(this.timer); this.request?.abort();
    if (!document.hidden && this.enabled) void this.poll();
  };
  private onPageHide = () => { clearTimeout(this.timer); this.request?.abort(); };
  private onPointerDown = (event: PointerEvent) => { this.pressed = event.button === 0 ? [event.clientX, event.clientY] : null; };
  private camera: THREE.Camera | null = null;
  private onPointerUp = (event: PointerEvent) => {
    if (!this.enabled || !this.camera || !this.pressed || Math.hypot(event.clientX - this.pressed[0], event.clientY - this.pressed[1]) > 6) return;
    this.pressed = null;
    const rect = this.canvas.getBoundingClientRect();
    this.pointer.set((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1);
    this.raycaster.setFromCamera(this.pointer, this.camera);
    const hit = this.raycaster.intersectObjects(this.group.children, true)[0];
    this.selected = hit?.object.parent?.userData.vehicleId ?? null;
    this.showDetails();
  };

  private async poll() {
    if (!this.enabled || document.hidden) return;
    const request = new AbortController();
    this.request = request;
    try {
      const response = await fetch(this.endpoint, { signal: AbortSignal.any([request.signal, AbortSignal.timeout(20000)]), cache: 'no-store' });
      const data: Snapshot = await response.json();
      if (!['live', 'stale', 'unavailable', 'unconfigured'].includes(data.state) || !Array.isArray(data.vehicles)) throw new Error('Invalid transit response');
      if (request.signal.aborted || this.request !== request) return;
      if (data.state === 'unconfigured' || data.state === 'unavailable') {
        if (!await this.activateSchedule(request) && !request.signal.aborted && this.request === request) {
          this.snapshot = data; this.reconcile(data);
        }
      } else {
        this.snapshot = data;
        this.reconcile(data);
      }
    } catch {
      if (request.signal.aborted || this.request !== request) return;
      if (this.snapshot && ['live', 'stale'].includes(this.snapshot.state) && this.markers.size) {
        this.snapshot = { ...this.snapshot, state: 'stale' };
      } else if (!await this.activateSchedule(request) && !request.signal.aborted && this.request === request) {
        this.snapshot = { state: 'unavailable', updatedAt: null, fetchedAt: null, vehicles: [] };
        this.reconcile(this.snapshot);
      }
    } finally {
      if (this.request === request && !request.signal.aborted) {
        this.request = null;
        this.showStatus();
        this.timer = window.setTimeout(() => void this.poll(), 30000);
      }
    }
  }

  private async activateSchedule(request: AbortController): Promise<boolean> {
    try {
      if (!this.schedule) {
        this.status.textContent = 'Loading timetable estimates…';
        this.loadingSchedule ??= fetchGzip('data/delijn-schedule.json.gz')
          .then((buffer) => new SchedulePlayer(JSON.parse(new TextDecoder().decode(buffer)) as ScheduleData))
          .finally(() => { this.loadingSchedule = null; });
        this.schedule = await this.loadingSchedule;
      }
      if (request.signal.aborted || this.request !== request) return true;
      this.snapshot = { state: 'scheduled', updatedAt: null, fetchedAt: new Date().toISOString(), vehicles: [] };
      this.updateSchedule();
      return true;
    } catch { return false; }
  }

  private updateSchedule() {
    if (!this.schedule || this.snapshot?.state !== 'scheduled') return;
    const now = Date.now(), estimatedAt = new Date(now).toISOString();
    this.snapshot.vehicles = this.schedule.positions(now).map((position) => ({ ...position, label: null,
      observedAt: null, estimated: true, estimatedAt, delaySeconds: null, canceled: false }));
    this.reconcile(this.snapshot);
    this.lastScheduleUpdate = performance.now();
  }

  private makeMarker(vehicle: Vehicle): Marker {
    const group = new THREE.Group();
    group.userData.vehicleId = vehicle.id;
    const material = new THREE.MeshStandardMaterial({ color: 0xf5d229, roughness: 0.7 });
    const body = new THREE.Mesh(this.geometry, material);
    body.castShadow = body.receiveShadow = true;
    body.scale.z = vehicle.kind === 'tram' ? 30 : vehicle.kind === 'bus' ? 12 : 7;
    const textureCanvas = document.createElement('canvas');
    textureCanvas.width = 256; textureCanvas.height = 64;
    const context = textureCanvas.getContext('2d')!;
    context.fillStyle = vehicle.estimated ? '#b9e3ec' : '#f5d229'; context.beginPath(); context.roundRect(0, 0, 256, 64, 16); context.fill();
    context.fillStyle = '#192127'; context.font = 'bold 30px sans-serif'; context.textAlign = 'center'; context.textBaseline = 'middle';
    context.fillText(`${vehicle.estimated ? '≈ ' : ''}${vehicle.routeName || 'De Lijn'}`, 128, 33, 236);
    const texture = new THREE.CanvasTexture(textureCanvas);
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, depthTest: true, transparent: true }));
    sprite.position.y = 9; sprite.scale.set(22, 5.5, 1);
    group.add(body, sprite); this.group.add(group);
    return { group, body, sprite, vehicle };
  }

  private remove(id: string) {
    const marker = this.markers.get(id);
    if (!marker) return;
    this.group.remove(marker.group);
    (marker.body.material as THREE.Material).dispose();
    const material = marker.sprite.material;
    material.map?.dispose(); material.dispose();
    this.markers.delete(id);
    if (this.selected === id) this.selected = null;
  }

  private reconcile(snapshot: Snapshot) {
    const seen = new Set<string>();
    for (const vehicle of snapshot.vehicles) {
      if (!Number.isFinite(vehicle.x) || !Number.isFinite(vehicle.y) || !Number.isFinite(Date.parse(positionTime(vehicle)))
        || vehicle.x < 0 || vehicle.y < 0 || vehicle.x >= this.world.W || vehicle.y >= this.world.D || age(positionTime(vehicle)) > 180000) continue;
      seen.add(vehicle.id);
      let marker = this.markers.get(vehicle.id);
      if (marker && (marker.vehicle.kind !== vehicle.kind || marker.vehicle.routeName !== vehicle.routeName || marker.vehicle.estimated !== vehicle.estimated)) { this.remove(vehicle.id); marker = undefined; }
      if (!marker) { marker = this.makeMarker(vehicle); this.markers.set(vehicle.id, marker); }
      marker.vehicle = vehicle;
      marker.group.position.set(vehicle.x, this.world.groundAt(vehicle.x, vehicle.y) + 0.15, vehicle.y);
      marker.body.rotation.y = Math.PI - (vehicle.bearing ?? 0) * Math.PI / 180;
      (marker.body.material as THREE.MeshStandardMaterial).color.setHex(vehicle.estimated ? 0x79b9cb : vehicle.canceled ? 0xd85c50 : 0xf5d229);
    }
    for (const id of this.markers.keys()) if (!seen.has(id)) this.remove(id);
    this.showDetails();
  }

  private showStatus() {
    if (!this.enabled) return;
    const snapshot = this.snapshot;
    if (!snapshot) return;
    if (snapshot.state === 'scheduled' && this.schedule) {
      this.panel.dataset.state = 'scheduled';
      this.toggle.textContent = 'De Lijn · scheduled estimates';
      this.status.textContent = this.schedule.expired(Date.now()) ? 'Timetable expired · refresh required'
        : this.schedule.data.validFrom && localServiceDate(Date.now()) < this.schedule.data.validFrom ? 'Timetable is not yet valid'
        : `${this.markers.size} scheduled ${this.markers.size === 1 ? 'trip' : 'trips'} · not live GPS`;
      this.source.href = this.schedule.data.sourcePage;
      this.source.textContent = `De Lijn timetable · ${scheduleClock.format(Date.now())} Brussels`;
      return;
    }
    this.toggle.textContent = 'De Lijn · live positions';
    this.source.href = 'https://portal.delijn.be/';
    const stale = snapshot.state === 'stale' || (snapshot.updatedAt && age(snapshot.updatedAt) > 90000)
      || [...this.markers.values()].some((marker) => age(positionTime(marker.vehicle)) > 90000);
    const count = `${this.markers.size} ${this.markers.size === 1 ? 'vehicle' : 'vehicles'}`;
    this.panel.dataset.state = stale ? 'stale' : snapshot.state;
    if (snapshot.state === 'unconfigured') this.status.textContent = 'Live transit not connected';
    else if (snapshot.state === 'unavailable') this.status.textContent = 'Live positions unavailable · retrying';
    else if (stale) this.status.textContent = `Last known positions · ${count}`;
    else this.status.textContent = this.markers.size ? `${count} in this area · live` : 'No recent vehicles in this area';
    this.source.textContent = `Source: De Lijn${snapshot.updatedAt ? ` · ${time(snapshot.updatedAt)}` : ''} · real time`;
  }

  private showDetails() {
    const vehicle = this.selected ? this.markers.get(this.selected)?.vehicle : null;
    this.detail.hidden = !vehicle || !this.enabled;
    if (!vehicle) return;
    if (vehicle.estimated) {
      this.detail.textContent = [`Line ${vehicle.routeName || vehicle.routeId}`, vehicle.headsign,
        'Scheduled estimate', 'Actual delays and cancellations unknown'].filter(Boolean).join(' · ');
      return;
    }
    const delay = vehicle.delaySeconds;
    const difference = delay === null ? '' : Math.abs(delay) < 60 ? `${Math.abs(delay)} sec` : `${Math.abs(Math.round(delay / 60))} min`;
    const timing = vehicle.canceled ? 'Trip canceled' : delay === null ? 'Delay not reported' : delay === 0 ? 'On time' : `${difference} ${delay > 0 ? 'late' : 'early'}`;
    this.detail.textContent = [vehicle.routeName ? `Line ${vehicle.routeName}` : vehicle.routeId ? `Route ${vehicle.routeId}` : 'De Lijn vehicle',
      `Vehicle ${vehicle.label || vehicle.id}`, timing, vehicle.observedAt ? `GPS ${time(vehicle.observedAt)}` : 'GPS time unknown'].join(' · ');
  }

  update(camera: THREE.Camera, ready: boolean) {
    this.camera = camera;
    this.group.visible = this.enabled && ready;
    if (this.enabled && this.snapshot?.state === 'scheduled' && performance.now() - this.lastScheduleUpdate > 200) this.updateSchedule();
    for (const [id, marker] of this.markers) {
      if (age(positionTime(marker.vehicle)) > 180000) { this.remove(id); continue; }
      marker.group.position.y = this.world.groundAt(marker.vehicle.x, marker.vehicle.y) + 0.15;
      const stale = !marker.vehicle.estimated && (this.snapshot?.state === 'stale' || (this.snapshot?.updatedAt && age(this.snapshot.updatedAt) > 90000) || age(positionTime(marker.vehicle)) > 90000);
      marker.sprite.material.opacity = stale ? 0.45 : 1;
      const material = marker.body.material as THREE.MeshStandardMaterial;
      if (material.transparent !== !!stale) { material.transparent = !!stale; material.needsUpdate = true; }
      material.opacity = stale ? 0.45 : 1;
      this.projected.copy(marker.group.position).project(camera);
      marker.sprite.visible = this.projected.z < 1;
    }
    if (performance.now() - this.lastStatus > 1000) { this.lastStatus = performance.now(); this.showStatus(); this.showDetails(); }
  }

  dispose() {
    this.enabled = false; clearTimeout(this.timer); this.request?.abort();
    document.removeEventListener('visibilitychange', this.onVisibility);
    this.canvas.removeEventListener('pointerdown', this.onPointerDown);
    this.canvas.removeEventListener('pointerup', this.onPointerUp);
    window.removeEventListener('pagehide', this.onPageHide);
    this.toggle.removeEventListener('click', this.onToggle);
    for (const id of this.markers.keys()) this.remove(id);
    this.geometry.dispose(); this.scene.remove(this.group); this.panel.remove();
  }
}
