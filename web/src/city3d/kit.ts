// Geometry helpers. Static things (buildings, roads, plaza, fences' posts, lamps) are merged into ONE
// mesh with vertex colours, so a whole district costs a single draw call. Repeated things (trees,
// bushes, rocks, flowers, fence segments, road arrows) are InstancedMeshes elsewhere.
import * as THREE from "three";
import { mergeGeometries } from "three/examples/jsm/utils/BufferGeometryUtils.js";
import type { XZ } from "./town";

const tmp = new THREE.Matrix4();
const q = new THREE.Quaternion();
const e = new THREE.Euler();

export class Batch {
  private parts: THREE.BufferGeometry[] = [];

  /** Add a geometry, coloured, placed at pos with rotation (radians) and optional scale. */
  add(g: THREE.BufferGeometry, color: string | number, pos: [number, number, number] = [0, 0, 0], rot: [number, number, number] = [0, 0, 0], scale: [number, number, number] = [1, 1, 1]): this {
    const geo = g.index ? g.toNonIndexed() : g.clone();
    geo.deleteAttribute("uv");
    tmp.compose(new THREE.Vector3(...pos), q.setFromEuler(e.set(...rot)), new THREE.Vector3(...scale));
    geo.applyMatrix4(tmp);
    const c = new THREE.Color(color);
    const n = geo.getAttribute("position").count;
    const arr = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) arr.set([c.r, c.g, c.b], i * 3);
    geo.setAttribute("color", new THREE.BufferAttribute(arr, 3));
    this.parts.push(geo);
    return this;
  }
  box(size: [number, number, number], color: string | number, pos: [number, number, number], rotY = 0): this {
    return this.add(new THREE.BoxGeometry(...size), color, pos, [0, rotY, 0]);
  }
  cyl(rTop: number, rBottom: number, h: number, seg: number, color: string | number, pos: [number, number, number]): this {
    return this.add(new THREE.CylinderGeometry(rTop, rBottom, h, seg), color, pos);
  }
  sphere(r: number, color: string | number, pos: [number, number, number], scale: [number, number, number] = [1, 1, 1]): this {
    return this.add(new THREE.SphereGeometry(r, 10, 7), color, pos, [0, 0, 0], scale);
  }
  get triangles(): number {
    return this.parts.reduce((n, g) => n + g.getAttribute("position").count / 3, 0);
  }
  build(): THREE.BufferGeometry {
    const g = mergeGeometries(this.parts, false)!;
    g.computeBoundingSphere();
    return g;
  }
}

/** A flat ribbon of the given width along a polyline, at height y (one quad per step). */
export function ribbon(points: XZ[], width: number, y: number): THREE.BufferGeometry {
  const n = points.length;
  const pos = new Float32Array(n * 2 * 3);
  const idx: number[] = [];
  for (let i = 0; i < n; i++) {
    const a = points[Math.max(0, i - 1)], b = points[Math.min(n - 1, i + 1)];
    let dx = b[0] - a[0], dz = b[1] - a[1];
    const l = Math.hypot(dx, dz) || 1;
    dx /= l;
    dz /= l;
    const nx = -dz * (width / 2), nz = dx * (width / 2);
    pos.set([points[i][0] + nx, y, points[i][1] + nz, points[i][0] - nx, y, points[i][1] - nz], i * 6);
    if (i > 0) idx.push(2 * (i - 1), 2 * i, 2 * (i - 1) + 1, 2 * (i - 1) + 1, 2 * i, 2 * i + 1); // faces up
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  g.setIndex(idx);
  g.computeVertexNormals();
  // normals must point up whatever the winding
  const nor = g.getAttribute("normal");
  for (let i = 0; i < nor.count; i++) nor.setXYZ(i, 0, 1, 0);
  return g;
}

/** Points every `step` metres along a polyline, with the heading (radians about y) at each. */
export function along(points: XZ[], step: number, offset = step / 2): { x: number; z: number; heading: number }[] {
  const out: { x: number; z: number; heading: number }[] = [];
  let next = offset;
  let walked = 0;
  for (let i = 1; i < points.length; i++) {
    const dx = points[i][0] - points[i - 1][0], dz = points[i][1] - points[i - 1][1];
    const len = Math.hypot(dx, dz);
    while (walked + len >= next && len > 0) {
      const t = (next - walked) / len;
      out.push({ x: points[i - 1][0] + dx * t, z: points[i - 1][1] + dz * t, heading: Math.atan2(dx, dz) });
      next += step;
    }
    walked += len;
  }
  return out;
}

/** A rounded rectangle slab (the town board), extruded downwards from y = 0. */
export function slab(w: number, d: number, corner: number, depth: number): THREE.BufferGeometry {
  const s = new THREE.Shape();
  const x = -w / 2, y = -d / 2;
  s.moveTo(x + corner, y);
  s.lineTo(x + w - corner, y);
  s.quadraticCurveTo(x + w, y, x + w, y + corner);
  s.lineTo(x + w, y + d - corner);
  s.quadraticCurveTo(x + w, y + d, x + w - corner, y + d);
  s.lineTo(x + corner, y + d);
  s.quadraticCurveTo(x, y + d, x, y + d - corner);
  s.lineTo(x, y + corner);
  s.quadraticCurveTo(x, y, x + corner, y);
  const g = new THREE.ExtrudeGeometry(s, { depth, bevelEnabled: false, curveSegments: 6 });
  g.rotateX(Math.PI / 2); // extrude along -y
  return g;
}
