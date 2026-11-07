import * as THREE from 'three';
import { el, fetchJson } from './util';
import type { World } from './world';

interface Tree {
  id: string; species: string | null; street: string | null; x: number; y: number;
  height_range_m: string | null; height_estimate_m: number | null; height_class: string | null;
  diameter_raw: number | null; trunk_circumference_raw: number | null; planting_date: string | null;
  manager: string | null; owner: string | null; protected: string | null;
  rendered_height_m: number; crown_radius_estimate_m: number; height_inferred: boolean; rendered_from: string;
}

const RANGE = 140, MAX = 400, HIT = 14;
const FROM: Record<string, string> = { dhsm: 'measured LiDAR crown (DHMV II)', schematic: 'schematic crown from height class' };

// A toggle (button or the T key) that marks every tree near the camera with a dot; hovering or tapping a dot
// shows the Groendienst inventory record behind it.
export class TreeInfo {
  on = false;
  private trees: Tree[] = [];
  private readonly canvas = el('canvas', 'treedots', '', document.body) as HTMLCanvasElement;
  private readonly card = el('div', 'treecard off', '', document.body);
  private readonly btn = el('button', 'treetoggle', 'Trees', document.body) as HTMLButtonElement;
  private hits: { t: Tree; x: number; y: number }[] = [];
  private pointer: [number, number] | null = null;
  private shown: Tree | null = null;
  private readonly v = new THREE.Vector3();

  constructor() {
    this.btn.title = 'Show tree metadata (T)';
    this.btn.setAttribute('aria-pressed', 'false');
    this.btn.addEventListener('click', () => this.set(!this.on));
    addEventListener('keydown', (e) => { if (e.key.toLowerCase() === 't' && !e.ctrlKey && !e.metaKey && !e.altKey && !/input|textarea/i.test((e.target as HTMLElement).tagName)) this.set(!this.on); });
    addEventListener('pointermove', (e) => { this.pointer = [e.clientX, e.clientY]; });
    addEventListener('pointerdown', (e) => { if (this.on) { this.pointer = [e.clientX, e.clientY]; this.pick(true); } });
    fetchJson<{ trees: Tree[] }>('data/trees.json').then((d) => { this.trees = d.trees; }).catch((e) => console.warn('trees', e));
  }

  set(on: boolean) {
    this.on = on;
    this.btn.classList.toggle('active', on);
    this.btn.setAttribute('aria-pressed', String(on));
    if (!on) { this.show(null); this.canvas.getContext('2d')?.clearRect(0, 0, this.canvas.width, this.canvas.height); }
  }

  update(camera: THREE.Camera, world: World | null) {
    if (!this.on || !world || !this.trees.length) return;
    const w = innerWidth, h = innerHeight, dpr = devicePixelRatio || 1;
    if (this.canvas.width !== w * dpr || this.canvas.height !== h * dpr) { this.canvas.width = w * dpr; this.canvas.height = h * dpr; this.canvas.style.width = `${w}px`; this.canvas.style.height = `${h}px`; }
    const g = this.canvas.getContext('2d')!;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    const cam = camera.position, near: { t: Tree; d: number }[] = [];
    for (const t of this.trees) {
      const d = Math.hypot(t.x - cam.x, t.y - cam.z);
      if (d < RANGE + cam.y * 0.5) near.push({ t, d });
    }
    near.sort((a, b) => a.d - b.d);
    this.hits = [];
    for (const { t } of near.slice(0, MAX)) {
      this.v.set(t.x + 0.5, world.groundAt(t.x, t.y) + t.rendered_height_m * 0.6, t.y + 0.5).project(camera);
      if (this.v.z >= 1 || Math.abs(this.v.x) > 1.05 || Math.abs(this.v.y) > 1.05) continue;
      const x = (this.v.x * 0.5 + 0.5) * w, y = (-this.v.y * 0.5 + 0.5) * h;
      g.beginPath(); g.arc(x, y, 4, 0, Math.PI * 2);
      g.fillStyle = t.rendered_from === 'dhsm' ? '#7fd36b' : '#f2c14e';
      g.fill(); g.lineWidth = 1.5; g.strokeStyle = 'rgba(0,0,0,.8)'; g.stroke();
      this.hits.push({ t, x, y });
    }
    this.pick(false);
  }

  private pick(sticky: boolean) {
    if (!this.pointer) return;
    let best: Tree | null = null, d = HIT ** 2;
    for (const k of this.hits) {
      const dd = (k.x - this.pointer[0]) ** 2 + (k.y - this.pointer[1]) ** 2;
      if (dd < d) { d = dd; best = k.t; }
    }
    if (best || sticky) this.show(best, this.pointer);
    else if (this.shown) this.show(null);
  }

  private show(t: Tree | null, at?: [number, number]) {
    if (t !== this.shown) {
      this.shown = t;
      this.card.classList.toggle('off', !t);
      if (t) this.card.innerHTML = this.html(t);
    }
    if (t && at) {
      const x = Math.min(at[0] + 14, innerWidth - 270), y = Math.min(at[1] + 14, innerHeight - this.card.offsetHeight - 8);
      this.card.style.transform = `translate(${Math.max(8, x)}px, ${Math.max(8, y)}px)`;
    }
  }

  private html(t: Tree) {
    const rows: [string, unknown][] = [
      ['Street', t.street], ['Height class', t.height_class], ['Height estimate', t.height_estimate_m && `${t.height_estimate_m} m`],
      ['Height range', t.height_range_m], ['Rendered height', `${t.rendered_height_m} m${t.height_inferred ? ' (inferred)' : ''}`],
      ['Crown radius', `${t.crown_radius_estimate_m} m (estimate)`], ['Rendered from', FROM[t.rendered_from] ?? t.rendered_from],
      ['Diameter (raw)', t.diameter_raw], ['Trunk circumf. (raw)', t.trunk_circumference_raw], ['Planted', t.planting_date],
      ['Protected', t.protected], ['Owner', t.owner], ['Manager', t.manager], ['Grid', `${t.x}, ${t.y}`], ['ID', t.id],
    ];
    const esc = (s: unknown) => String(s).replace(/[&<>"]/g, (c) => `&#${c.charCodeAt(0)};`);
    return `<b>${esc(t.species ?? 'Unknown species')}</b><dl>${rows.filter((r) => r[1] != null && r[1] !== '').map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`;
  }
}
