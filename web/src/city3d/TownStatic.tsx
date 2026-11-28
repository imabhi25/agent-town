// The static town, after the owner's mockup: a dark board, cobblestone streets round a paved square
// with a fountain and a town hall, lawns for the six blocks of cottage lots, pine trees, glowing
// lamps. All of it is drawn from town-layout.json; nothing here is positioned by hand.
//
// Draw-call budget: one vertex-coloured batch for everything solid that never changes (lawns,
// curbs, fountain, town hall, lamp posts), one textured mesh for all cobblestones, one unlit batch
// for everything that glows (windows, lanterns), instanced trees, bushes, flowers and rocks, and
// one Points object for the lamp halos.
import { Html } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useLayoutEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { mergeGeometries } from "three/examples/jsm/utils/BufferGeometryUtils.js";
import { Batch } from "./kit";
import { C, ZONE_COLORS } from "./palette3d";
import { LAYOUT, type PropSpec, zoneLabelAt } from "./town";

export type Quality = "high" | "low";
const LOW_TREES = 18;

/** A small low-poly robot built into a batch: boxy body, visor, antenna; the Foreman wears a hard hat. */
export function robot(b: Batch, x: number, z: number, rotY: number, s: number, accent: string, hat = false): void {
  const c = Math.cos(rotY), sn = Math.sin(rotY);
  const at = (lx: number, ly: number, lz: number): [number, number, number] => [x + (lx * c + lz * sn) * s, ly * s, z + (-lx * sn + lz * c) * s];
  const box = (sx: number, sy: number, sz: number, lx: number, ly: number, lz: number, color: string) =>
    b.add(new THREE.BoxGeometry(sx * s, sy * s, sz * s), color, at(lx, ly, lz), [0, rotY, 0]);
  box(0.46, 0.42, 0.34, 0, 0.42, 0, "#f4f1f8");
  box(0.22, 0.16, 0.02, 0, 0.46, 0.175, accent);
  box(0.4, 0.32, 0.34, 0, 0.82, 0, "#ffffff");
  box(0.3, 0.14, 0.02, 0, 0.83, 0.175, accent);
  box(0.1, 0.34, 0.12, -0.3, 0.42, 0, "#d9d3e3");
  box(0.1, 0.34, 0.12, 0.3, 0.42, 0, "#d9d3e3");
  box(0.14, 0.2, 0.16, -0.12, 0.1, 0, "#b7b0c4");
  box(0.14, 0.2, 0.16, 0.12, 0.1, 0, "#b7b0c4");
  if (hat) {
    box(0.46, 0.1, 0.4, 0, 1.03, 0, C.hardhat);
    box(0.3, 0.1, 0.3, 0, 1.12, 0, C.hardhat);
  } else {
    box(0.04, 0.14, 0.04, 0, 1.06, 0, "#b7b0c4");
    b.sphere(0.07 * s, accent, at(0, 1.17, 0));
  }
}

/** Which zones are in use and how many tasks each holds (the preview draws ghost lots from it). */
export type Plan = { stage: number; tasks: number }[];

/** A gable roof: a 3-sided prism along x with its ridge on top (apex at y = +1, base at y = -0.5). */
export const GABLE = () => new THREE.CylinderGeometry(1, 1, 1, 3, 1).rotateZ(Math.PI / 2).rotateX(-Math.PI / 2);

// ---- cobblestones ---------------------------------------------------------------------------------

/** A tileable cobblestone texture drawn once on a canvas (no image files to load). */
function cobbleTexture(): THREE.Texture | null {
  const cv = document.createElement("canvas");
  cv.width = cv.height = 256;
  const g = cv.getContext("2d");
  if (!g) return null;
  g.fillStyle = C.mortar;
  g.fillRect(0, 0, 256, 256);
  let seed = 7;
  const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const rows = 8, cols = 8, cw = 256 / cols, ch = 256 / rows;
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const x = c * cw + (r % 2 ? cw / 2 : 0), y = r * ch;
      const base = new THREE.Color(C.cobble).multiplyScalar(0.86 + rnd() * 0.24);
      g.fillStyle = `#${base.getHexString()}`;
      for (const dx of [0, -256]) {
        g.beginPath();
        g.roundRect(x + dx + 2, y + 2, cw - 4, ch - 4, 7);
        g.fill();
      }
    }
  }
  const t = new THREE.CanvasTexture(cv);
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 4;
  return t;
}

/** A flat rectangle from a to b (width w) at height y, with UVs in world metres / `tile`. */
function strip(a: [number, number], b: [number, number], w: number, y: number, tile: number, extend = 0): THREE.BufferGeometry {
  const len = Math.hypot(b[0] - a[0], b[1] - a[1]) + extend;
  const g = new THREE.PlaneGeometry(len, w).rotateX(-Math.PI / 2);
  g.rotateY(-Math.atan2(b[1] - a[1], b[0] - a[0]));
  g.translate((a[0] + b[0]) / 2, y, (a[1] + b[1]) / 2);
  const pos = g.getAttribute("position"), uv = g.getAttribute("uv");
  for (let i = 0; i < pos.count; i++) uv.setXY(i, pos.getX(i) / tile, pos.getZ(i) / tile);
  return g;
}

function Cobbles() {
  const tex = useMemo(cobbleTexture, []);
  const geo = useMemo(() => {
    const parts: THREE.BufferGeometry[] = [];
    const { plaza } = LAYOUT;
    parts.push(strip([-plaza.w / 2, 0], [plaza.w / 2, 0], plaza.d, 0.06, 1.6)); // the square
    for (const r of LAYOUT.roads) {
      if (r.type === "street") parts.push(strip(r.points[0], r.points[1], r.width, 0.07, 1.1, r.width));
      if (r.type === "lane") parts.push(strip(r.points[0], r.points[1], r.width, 0.07, 1.1, 0.4));
      if (r.id.startsWith("in")) parts.push(strip(r.points[0], r.points[1], 0.8, 0.075, 1.1));
    }
    return mergeGeometries(parts, false)!;
  }, []);
  return (
    <mesh geometry={geo} receiveShadow>
      <meshStandardMaterial map={tex} color={tex ? "#ffffff" : C.cobble} roughness={0.95} />
    </mesh>
  );
}

// ---- solid batch and glowing batch ------------------------------------------------------------------

function useStaticGeometry(plan: Plan, preview: boolean) {
  return useMemo(() => {
    const b = new Batch();
    const glow = new Batch();
    const { zones, plaza } = LAYOUT;

    // lawns for the blocks, and a darker edge round the square and every street (the curb)
    zones.forEach((z) => b.box([z.w, 0.05, z.d], C.lawn, [z.x, 0.025, z.z]));
    b.box([plaza.w + 0.5, 0.05, plaza.d + 0.5], C.curb, [0, 0.03, 0]);
    for (const r of LAYOUT.roads) {
      if (r.type !== "street" && r.type !== "lane") continue;
      const [a, c] = r.points;
      const len = Math.hypot(c[0] - a[0], c[1] - a[1]) + r.width;
      b.add(new THREE.BoxGeometry(len, 0.05, r.width + 0.35), C.curb, [(a[0] + c[0]) / 2, 0.035, (a[1] + c[1]) / 2], [0, -Math.atan2(c[1] - a[1], c[0] - a[0]), 0]);
    }

    // the fountain: an octagonal stone basin, water, a column with a bowl
    const [fx, fz] = plaza.fountain;
    b.cyl(1.35, 1.45, 0.45, 8, C.stone, [fx, 0.28, fz]);
    b.cyl(0.16, 0.22, 1.2, 8, C.stone, [fx, 0.9, fz]);
    b.cyl(0.55, 0.3, 0.18, 8, C.stone, [fx, 1.45, fz]);
    b.cyl(0.08, 0.08, 0.35, 6, C.stone, [fx, 1.7, fz]);
    glow.cyl(1.12, 1.12, 0.06, 8, C.water, [fx, 0.48, fz]);
    glow.cyl(0.42, 0.42, 0.02, 8, C.water, [fx, 1.55, fz]);

    // the town hall: stone base, cream walls, dark roof, a clock tower with a spire
    const h = plaza.hall;
    const wallH = 2.1;
    const front = h.z + h.d / 2;
    const towerZ = front - 0.75;
    b.box([h.w + 0.3, 0.3, h.d + 0.3], C.stone, [h.x, 0.2, h.z]);
    b.box([h.w, wallH, h.d], C.wall, [h.x, 0.35 + wallH / 2, h.z]);
    b.add(GABLE(), C.roofDark, [h.x, 0.35 + wallH + 0.55 * 0.5, h.z], [0, 0, 0], [h.w + 0.4, 0.55 * 1.5, (h.d + 0.5) / 1.732]);
    b.box([1.5, 1.6, 1.5], C.wall, [h.x, 0.35 + wallH + 0.8, towerZ]);
    b.add(new THREE.ConeGeometry(1.25, 1.6, 4), C.roofDark, [h.x, 0.35 + wallH + 2.4, towerZ], [0, Math.PI / 4, 0]);
    b.box([0.06, 0.6, 0.06], "#c9a55a", [h.x, 0.35 + wallH + 3.5, towerZ]);
    b.box([0.9, 1.3, 0.08], C.door, [h.x, 0.35 + 0.65, front + 0.01]);
    for (const [dx, y] of [[-1.85, 0.95], [-1.05, 0.95], [1.05, 0.95], [1.85, 0.95], [-1.85, 1.75], [-1.05, 1.75], [1.05, 1.75], [1.85, 1.75]]) {
      glow.box([0.42, 0.52, 0.04], C.window, [h.x + dx, 0.35 + y, front + 0.02]);
    }

    // the Foreman's podium in front of the hall
    b.cyl(plaza.podium_r, plaza.podium_r + 0.08, 0.16, 12, C.stone, [plaza.foreman[0], 0.1, plaza.foreman[1]]);

    // lamp posts with lanterns
    for (const p of LAYOUT.props) {
      if (p.kind !== "lamp") continue;
      b.cyl(0.05, 0.07, 1.7, 6, C.iron, [p.x, 0.85, p.z]);
      b.box([0.3, 0.06, 0.3], C.iron, [p.x, 1.74, p.z]);
      glow.box([0.22, 0.28, 0.22], C.lamp, [p.x, 1.92, p.z]);
      b.add(new THREE.ConeGeometry(0.22, 0.18, 4), C.iron, [p.x, 2.15, p.z], [0, Math.PI / 4, 0]);
    }

    // Phase A preview only: ghost foundations. Live, each building draws its own (it changes with status).
    zones.forEach((z) => {
      const used = preview ? plan.find((p) => p.stage === z.stage) : undefined;
      z.slots.slice(0, used?.tasks ?? 0).forEach((s) => b.box([s.size, 0.07, s.size], C.ghost, [s.x, 0.1, s.z], (s.rot * Math.PI) / 180));
    });
    return { geo: b.build(), glowGeo: glow.build() };
  }, [plan, preview]);
}

/** The clock on the town hall tower: a glowing disc facing the viewer. */
function Clock() {
  const h = LAYOUT.plaza.hall;
  return (
    <mesh position={[h.x, 0.35 + 2.1 + 0.85, h.z + h.d / 2 + 0.02]}>
      <circleGeometry args={[0.42, 20]} />
      <meshBasicMaterial color="#ffe6b0" toneMapped={false} />
    </mesh>
  );
}

/** The Foreman in the square: one batched mesh that turns (eased) to face the building the latest
 * event was about. `look` null = keep facing where it last looked. */
export function Foreman({ look }: { look?: [number, number] | null }) {
  const [fx, fz] = LAYOUT.plaza.foreman;
  const geo = useMemo(() => {
    const b = new Batch();
    robot(b, 0, 0, 0, 1.5, C.lavender, true);
    return b.build();
  }, []);
  const ref = useRef<THREE.Group>(null);
  const want = useRef(0);
  if (look) want.current = Math.atan2(look[0] - fx, look[1] - fz);
  useFrame((_, dt) => {
    const g = ref.current;
    if (!g) return;
    const d = Math.atan2(Math.sin(want.current - g.rotation.y), Math.cos(want.current - g.rotation.y)); // shortest way round
    if (Math.abs(d) > 1e-3) g.rotation.y += d * Math.min(1, dt * 4);
  });
  return (
    <group ref={ref} position={[fx, 0.18, fz]}>
      <mesh geometry={geo} castShadow>
        <meshStandardMaterial vertexColors roughness={0.8} flatShading />
      </mesh>
    </group>
  );
}

/** Soft halos round the lanterns (one Points object). Dimmer while the town is being built, bright
 * once the goal succeeds. */
function Halos({ lit }: { lit: boolean }) {
  const lamps = useMemo(() => LAYOUT.props.filter((p) => p.kind === "lamp"), []);
  const geo = useMemo(() => {
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(lamps.flatMap((p) => [p.x, 1.92, p.z]), 3));
    return g;
  }, [lamps]);
  const tex = useMemo(() => {
    const cv = document.createElement("canvas");
    cv.width = cv.height = 64;
    const g = cv.getContext("2d");
    if (!g) return null;
    const grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
    grad.addColorStop(0, "rgba(255,214,140,0.9)");
    grad.addColorStop(0.35, "rgba(255,190,110,0.35)");
    grad.addColorStop(1, "rgba(255,170,90,0)");
    g.fillStyle = grad;
    g.fillRect(0, 0, 64, 64);
    return new THREE.CanvasTexture(cv);
  }, []);
  const mat = useRef<THREE.PointsMaterial>(null);
  useFrame((_, dt) => {
    const m = mat.current;
    if (!m) return;
    const want = lit ? 1 : 0.6;
    if (Math.abs(m.opacity - want) > 1e-3) m.opacity += (want - m.opacity) * Math.min(1, dt * 2);
  });
  if (!tex) return null;
  return (
    <points geometry={geo}>
      <pointsMaterial ref={mat} map={tex} size={2.6} sizeAttenuation transparent opacity={0.6} depthWrite={false} blending={THREE.AdditiveBlending} toneMapped={false} />
    </points>
  );
}

/** Warm light pooling on the square from its lamps (a few point lights; none on low quality). */
function SquareLights({ quality, lit }: { quality: Quality; lit: boolean }) {
  const { plaza } = LAYOUT;
  if (quality === "low") return null;
  const spots: [number, number][] = [
    [-plaza.w / 2 + 0.6, -1.5], [plaza.w / 2 - 0.6, -1.5], [-plaza.w / 2 + 0.6, 3], [plaza.w / 2 - 0.6, 3],
    [0, plaza.hall.z + plaza.hall.d / 2 + 1],
  ];
  return (
    <>
      {spots.map(([x, z], i) => (
        <pointLight key={i} position={[x, 2.4, z]} color="#ffc27a" intensity={lit ? 9 : 6} distance={9} decay={2} />
      ))}
    </>
  );
}

/** Instanced props of one kind: one draw call for all of them. */
function Instances({ props, geometry, color, colors, y, quality, limit }: {
  props: PropSpec[];
  geometry: THREE.BufferGeometry;
  color?: string;
  colors?: string[];
  y: (p: PropSpec) => number;
  quality: Quality;
  limit?: number;
}) {
  const ref = useRef<THREE.InstancedMesh>(null);
  const list = quality === "low" && limit !== undefined ? props.slice(0, limit) : props;
  useLayoutEffect(() => {
    const m = ref.current!;
    const mat = new THREE.Matrix4(), col = new THREE.Color();
    list.forEach((p, i) => {
      const turn = p.rot !== undefined ? (p.rot * Math.PI) / 180 : (p.x * 37) % (Math.PI * 2);
      mat.compose(new THREE.Vector3(p.x, y(p), p.z), new THREE.Quaternion().setFromEuler(new THREE.Euler(0, turn, 0)), new THREE.Vector3(p.s, p.s, p.s));
      m.setMatrixAt(i, mat);
      if (colors) m.setColorAt(i, col.set(colors[(p.c ?? 0) % colors.length]));
    });
    m.count = list.length;
    m.instanceMatrix.needsUpdate = true;
    if (m.instanceColor) m.instanceColor.needsUpdate = true;
  }, [list, colors, y]);
  return (
    <instancedMesh ref={ref} args={[geometry, undefined, Math.max(1, props.length)]} castShadow receiveShadow>
      <meshStandardMaterial color={color ?? "#ffffff"} roughness={0.9} flatShading />
    </instancedMesh>
  );
}

const PINE_COLORS = ["#2b5142", "#31594a"];
const BUSH_COLORS = ["#3f6a45", "#4a7550"];
const FLOWER_COLORS = ["#f0a0c0", "#c8a8ff", "#fff3e0", "#ffd479"];
const atGround = () => 0;
const crownY = (p: PropSpec) => 1.15 * p.s;
const trunkY = (p: PropSpec) => 0.3 * p.s;
const bushY = (p: PropSpec) => 0.25 * p.s;
const rockY = (p: PropSpec) => 0.18 * p.s;
const flowerY = () => 0.12;

function Decoration({ quality }: { quality: Quality }) {
  const g = useMemo(() => ({
    // a pine: three stacked cones, merged
    pine: mergeGeometries([
      new THREE.ConeGeometry(0.75, 1.1, 7).translate(0, 0.95, 0),
      new THREE.ConeGeometry(0.58, 0.95, 7).translate(0, 1.5, 0),
      new THREE.ConeGeometry(0.38, 0.8, 7).translate(0, 2.0, 0),
    ])!,
    crown: new THREE.IcosahedronGeometry(0.7, 1),
    trunk: new THREE.CylinderGeometry(0.08, 0.11, 0.6, 6),
    bush: new THREE.IcosahedronGeometry(0.38, 0),
    rock: new THREE.IcosahedronGeometry(0.4, 0).scale(1, 0.6, 0.8),
    flower: new THREE.BoxGeometry(0.12, 0.12, 0.12),
  }), []);
  const p = useMemo(() => {
    const by = (k: string) => LAYOUT.props.filter((x) => x.kind === k);
    const trees = by("tree");
    return {
      trees,
      pines: trees.filter((t) => (t.c ?? 0) < 2), // c 0-1: pines, c 2: a round tree
      round: trees.filter((t) => (t.c ?? 0) === 2),
      bush: by("bush"),
      rock: by("rock"),
      flower: by("flower"),
    };
  }, []);
  return (
    <>
      <Instances props={p.pines} geometry={g.pine} colors={PINE_COLORS} y={atGround} quality={quality} limit={LOW_TREES} />
      <Instances props={p.round} geometry={g.crown} color="#3a6448" y={crownY} quality={quality} limit={LOW_TREES} />
      <Instances props={p.trees} geometry={g.trunk} color="#5a4333" y={trunkY} quality={quality} limit={LOW_TREES * 2} />
      {quality === "high" && (
        <>
          <Instances props={p.bush} geometry={g.bush} colors={BUSH_COLORS} y={bushY} quality={quality} />
          <Instances props={p.rock} geometry={g.rock} color="#6e6878" y={rockY} quality={quality} />
          <Instances props={p.flower} geometry={g.flower} colors={FLOWER_COLORS} y={flowerY} quality={quality} />
        </>
      )}
    </>
  );
}

/** Gardens on the lots no task is using: a pine and two bushes each, so the town never looks empty.
 * When a task takes a lot, its garden makes way for the building. One draw call per kind. */
function Gardens({ taken }: { taken: Set<string> }) {
  const free = useMemo(() => LAYOUT.zones.flatMap((z) => z.slots).filter((s) => !taken.has(s.id)), [taken]);
  const pine = useMemo(() => mergeGeometries([
    new THREE.ConeGeometry(0.62, 0.95, 7).translate(0, 0.75, 0),
    new THREE.ConeGeometry(0.46, 0.8, 7).translate(0, 1.2, 0),
    new THREE.ConeGeometry(0.3, 0.65, 7).translate(0, 1.6, 0),
    new THREE.CylinderGeometry(0.07, 0.09, 0.4, 6).translate(0, 0.2, 0),
  ])!, []);
  const bush = useMemo(() => new THREE.IcosahedronGeometry(0.3, 0), []);
  const pines = useRef<THREE.InstancedMesh>(null);
  const bushes = useRef<THREE.InstancedMesh>(null);
  useLayoutEffect(() => {
    const m = new THREE.Matrix4(), q = new THREE.Quaternion(), one = new THREE.Vector3(1, 1, 1);
    free.forEach((s, i) => {
      const k = Number(s.id.split("_s")[1]);
      pines.current!.setMatrixAt(i, m.compose(new THREE.Vector3(s.x + (k % 2 ? -0.35 : 0.35), 0, s.z - 0.2), q, one.clone().setScalar(0.85 + (k % 3) * 0.1)));
      bushes.current!.setMatrixAt(2 * i, m.compose(new THREE.Vector3(s.x + (k % 2 ? 0.45 : -0.45), 0.2, s.z + 0.35), q, one));
      bushes.current!.setMatrixAt(2 * i + 1, m.compose(new THREE.Vector3(s.x + (k % 2 ? 0.05 : -0.05), 0.16, s.z + 0.55), q, one.clone().setScalar(0.7)));
    });
    pines.current!.count = free.length;
    bushes.current!.count = free.length * 2;
    pines.current!.instanceMatrix.needsUpdate = true;
    bushes.current!.instanceMatrix.needsUpdate = true;
  }, [free]);
  const all = LAYOUT.zones.length * 8;
  return (
    <>
      <instancedMesh ref={pines} args={[pine, undefined, all]} castShadow>
        <meshStandardMaterial color="#2e5847" roughness={0.9} flatShading />
      </instancedMesh>
      <instancedMesh ref={bushes} args={[bush, undefined, all * 2]} castShadow>
        <meshStandardMaterial color="#47724c" roughness={0.9} flatShading />
      </instancedMesh>
    </>
  );
}

/** A small boom barrier across a block entrance. `closed` 0 = raised (open), 1 = lowered. */
export function Barrier({ x, z, rot, closed = 0, scale = 0.5 }: { x: number; z: number; rot: number; closed?: number; scale?: number }) {
  const ang = -(rot * Math.PI) / 180;
  const arm = useRef<THREE.Group>(null);
  const lamp = useRef<THREE.MeshStandardMaterial>(null);
  const eased = useRef(closed);
  // Ease toward the target so the arm swings instead of jumping; nothing moves unless `closed` changes.
  useFrame((_, dt) => {
    const d = closed - eased.current;
    if (Math.abs(d) < 1e-3 && eased.current === closed) return;
    eased.current = Math.abs(d) < 1e-3 ? closed : eased.current + d * Math.min(1, dt * 5);
    if (arm.current) arm.current.rotation.x = -(Math.PI * 0.42) * (1 - eased.current);
    if (lamp.current) {
      const c = eased.current > 0.5 ? "#e8707a" : "#6cc788";
      lamp.current.color.set(c);
      lamp.current.emissive.set(c);
    }
  });
  return (
    <group position={[x, 0.06, z]} rotation={[0, ang, 0]} scale={scale}>
      <mesh position={[0, 0.6, -1.5]} castShadow>
        <boxGeometry args={[0.45, 1.2, 0.45]} />
        <meshStandardMaterial color={C.iron} roughness={0.8} flatShading />
      </mesh>
      <mesh position={[0, 1.3, -1.5]}>
        <sphereGeometry args={[0.2, 8, 6]} />
        <meshStandardMaterial ref={lamp} color="#6cc788" emissive="#6cc788" emissiveIntensity={0.9} />
      </mesh>
      <group ref={arm} position={[0, 1.05, -1.5]} rotation={[-(Math.PI * 0.42) * (1 - closed), 0, 0]}>
        {[0, 1, 2, 3].map((i) => (
          <mesh key={i} position={[0, 0, 0.35 + i * 0.7]} castShadow>
            <boxGeometry args={[0.18, 0.18, 0.7]} />
            <meshStandardMaterial color={i % 2 ? "#c4525c" : "#efe8dc"} roughness={0.7} flatShading />
          </mesh>
        ))}
      </group>
    </group>
  );
}

export function TownStatic({ quality = "high", plan, taken, circuitOpen = false, foremanLook = null, built = false }: {
  quality?: Quality;
  plan?: Plan;
  taken?: Set<string>; // lot ids that hold a task's building (the rest show a garden)
  circuitOpen?: boolean;
  foremanLook?: [number, number] | null; // where the Foreman looks (the latest event's building)
  built?: boolean; // the goal succeeded: the lamps brighten
}) {
  // No plan given = the Phase A preview (happy-path layout with ghosts and labels).
  const preview = plan === undefined;
  const used = plan ?? LAYOUT.preview.zones;
  const { geo, glowGeo } = useStaticGeometry(used, preview);
  // In the preview the planned lots are the first ones of each used block.
  const lots = useMemo(
    () => taken ?? new Set(used.flatMap((p) => LAYOUT.zones[p.stage - 1].slots.slice(0, p.tasks).map((s) => s.id))),
    [taken, used],
  );
  const { board, zones, plaza } = LAYOUT;
  const top = useMemo(() => new THREE.BoxGeometry(board.w, 0.6, board.d).translate(0, -0.3, 0), [board]);
  return (
    <>
      <mesh geometry={top} receiveShadow>
        <meshStandardMaterial color={C.ground} roughness={0.95} />
      </mesh>
      <mesh geometry={geo} receiveShadow castShadow>
        <meshStandardMaterial vertexColors roughness={0.85} flatShading />
      </mesh>
      <mesh geometry={glowGeo}>
        <meshBasicMaterial vertexColors toneMapped={false} />
      </mesh>
      <Cobbles />
      <Clock />
      <Foreman look={foremanLook} />
      <Halos lit={built} />
      <SquareLights quality={quality} lit={built} />
      <Decoration quality={quality} />
      <Gardens taken={lots} />
      {LAYOUT.gates.map((g) => (
        <Barrier key={g.id} x={g.x} z={g.z} rot={g.rot} closed={circuitOpen ? 1 : 0} />
      ))}
      {/* labels: block name with a step badge and a built counter; the Foreman */}
      {preview && LAYOUT.preview.zones.map((p) => {
        const z = zones[p.stage - 1];
        return (
          <Html key={z.id} position={zoneLabelAt(z)} center zIndexRange={[10, 0]} className="label3d zone">
            <b className="badge" style={{ background: ZONE_COLORS[p.stage - 1] }}>{p.stage}</b>
            <strong>{p.name}</strong>
            <span className="count3d">0/{p.tasks} built</span>
          </Html>
        );
      })}
      <Html position={[plaza.foreman[0], 2.6, plaza.foreman[1]]} center zIndexRange={[10, 0]} className="label3d small">
        <strong>Foreman</strong>
      </Html>
    </>
  );
}
