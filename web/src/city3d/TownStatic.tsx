// The static town (Phase A): board, zones with ghost foundations, roads and arrows, the plaza with
// the Foreman and idle workers, barriers, labels and decoration. All of it is drawn from
// town-layout.json; nothing here is positioned by hand. Phase B drives the same objects from events.
import { Html } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useLayoutEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { Batch, along, ribbon, slab } from "./kit";
import { C, ZONE_COLORS } from "./palette3d";
import { LAYOUT, ROADS_SAMPLED, type PropSpec } from "./town";

export type Quality = "high" | "low";
const LOW_TREES = 6;

const blend = (a: string, b: string, t: number) => `#${new THREE.Color(a).lerp(new THREE.Color(b), t).getHexString()}`;

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

/** Which zones are in use and how many tasks each holds: drives the tint and the footpaths to slots. */
export type Plan = { stage: number; tasks: number }[];

function useStaticGeometry(plan: Plan, preview: boolean): { geo: THREE.BufferGeometry; triangles: number } {
  return useMemo(() => {
    const b = new Batch();
    const { zones, plaza } = LAYOUT;
    const active = new Set(plan.map((p) => p.stage));

    // zone discs: a cream footpath rim, then grass tinted with the zone colour (pale when unused)
    zones.forEach((z, i) => {
      const used = active.has(z.stage);
      b.cyl(z.r + 0.3, z.r + 0.3, 0.035, 40, C.foot, [z.x, 0.0175, z.z]);
      b.cyl(z.r, z.r, 0.04, 40, used ? blend(C.zoneGrass, ZONE_COLORS[i], 0.45) : C.zoneGrass, [z.x, 0.02, z.z]);
    });

    // roads: a curb under the surface, footpaths on top
    // (footpaths inside a zone are drawn only to slots that have a planned task)
    const shown = new Set<string>(["in1", "in2", "in3", "in4", "in5", "in6", "foot_foreman"].filter((id) => !id.startsWith("in") || active.has(Number(id.slice(2)))));
    for (const z of zones) {
      const used = plan.find((p) => p.stage === z.stage);
      z.slots.slice(0, used?.tasks ?? 0).forEach((_, k) => shown.add(`${z.id}_p${k + 1}`));
    }
    for (const { road, pts } of ROADS_SAMPLED) {
      if (road.type === "foot") {
        if (shown.has(road.id) || road.id.startsWith("foot_pad")) b.add(ribbon(pts, Math.min(road.width, 0.55), 0.07), C.foot);
      }
      else {
        b.add(ribbon(pts, road.width + 0.5, 0.045), C.curb);
        b.add(ribbon(pts, road.width, 0.055), road.type === "ring" ? C.ringRoad : road.type === "spoke" ? C.spokeRoad : C.plaza);
      }
    }

    // plaza: disc, the Foreman's podium, idle pads, a flagpole
    b.cyl(plaza.r, plaza.r, 0.05, 40, C.plaza, [plaza.x, 0.025, plaza.z]);
    b.cyl(plaza.podium_r, plaza.podium_r + 0.1, 0.3, 16, "#a69cb5", [plaza.foreman[0], 0.15, plaza.foreman[1]]);
    plaza.pads.forEach(([px, pz]) => b.cyl(0.45, 0.45, 0.03, 14, "#9d93ab", [px, 0.065, pz]));
    // (the Foreman is its own mesh, <Foreman>, because it turns to watch the latest event)
    b.box([0.07, 3.1, 0.07], "#b8b1c2", [plaza.foreman[0] + 1.3, 1.55, plaza.foreman[1] - 0.6]);
    b.box([0.9, 0.5, 0.04], "#f2c14e", [plaza.foreman[0] + 1.75, 2.8, plaza.foreman[1] - 0.6]);
    // Phase A preview only: idle workers on the pads. Live, TownLive draws every worker (it moves them).
    if (preview) {
      [0, 2, 4].forEach((k) => {
        const [px, pz] = plaza.pads[k];
        robot(b, px, pz, Math.atan2(-px, -pz), 1.15, "#b7b0c4");
      });
    }

    // zones: signpost at the hub, ghost foundations for the planned tasks
    zones.forEach((z, i) => {
      b.box([0.08, 1.5, 0.08], "#b8b1c2", [z.x, 0.75, z.z]);
      b.box([0.7, 0.36, 0.05], ZONE_COLORS[i], [z.x + 0.4, 1.3, z.z]);
      // Phase A preview only: ghost foundations. Live, each building draws its own (it changes with status).
      const used = preview ? plan.find((p) => p.stage === z.stage) : undefined;
      z.slots.slice(0, used?.tasks ?? 0).forEach((s) => {
        const rot = (s.rot * Math.PI) / 180;
        b.box([s.size, 0.07, s.size], C.ghost, [s.x, 0.1, s.z], rot);
        const e = s.size / 2 - 0.06;
        for (const [dx, dz, w, d] of [[0, -e, s.size, 0.1], [0, e, s.size, 0.1], [-e, 0, 0.1, s.size], [e, 0, 0.1, s.size]]) {
          const c = Math.cos(rot), sn = Math.sin(rot);
          b.box([w, 0.1, d], C.ghostEdge, [s.x + dx * c + dz * sn, 0.13, s.z - dx * sn + dz * c], rot);
        }
      });
    });

    // lamp posts (the bulbs are <Lamps>: they glow, and brighten when the town is built)
    for (const p of LAYOUT.props) {
      if (p.kind !== "lamp") continue;
      b.cyl(0.05, 0.06, 1.4, 6, "#4b4458", [p.x, 0.7, p.z]);
    }
    return { geo: b.build(), triangles: b.triangles };
  }, [plan, preview]);
}

/** Instanced props of one kind: one draw call for all trees' crowns, one for trunks, and so on. */
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
      mat.compose(new THREE.Vector3(p.x, y(p), p.z), new THREE.Quaternion().setFromEuler(new THREE.Euler(0, ((p.rot ?? 0) * Math.PI) / 180, 0)), new THREE.Vector3(p.s, p.s, p.s));
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

function Decoration({ quality }: { quality: Quality }) {
  const by = (k: string) => LAYOUT.props.filter((p) => p.kind === k);
  const g = useMemo(() => ({
    crown: new THREE.IcosahedronGeometry(0.62, 1),
    trunk: new THREE.CylinderGeometry(0.07, 0.1, 0.6, 6),
    bush: new THREE.IcosahedronGeometry(0.4, 0),
    rock: new THREE.IcosahedronGeometry(0.4, 0).scale(1, 0.6, 0.8),
    flower: new THREE.BoxGeometry(0.12, 0.12, 0.12),
    fence: new THREE.BoxGeometry(1.15, 0.45, 0.07),
  }), []);
  const trees = by("tree");
  return (
    <>
      <Instances props={trees} geometry={g.crown} colors={["#3f6e5e", "#4d7a55", "#36695c"]} y={(p) => 1.05 * p.s} quality={quality} limit={LOW_TREES} />
      <Instances props={trees} geometry={g.trunk} color="#6b5444" y={(p) => 0.3 * p.s} quality={quality} limit={LOW_TREES} />
      {quality === "high" && (
        <>
          <Instances props={by("bush")} geometry={g.bush} colors={["#4f7a4c", "#5a8556"]} y={(p) => 0.3 * p.s} quality={quality} />
          <Instances props={by("rock")} geometry={g.rock} color="#6e6878" y={(p) => 0.2 * p.s} quality={quality} />
          <Instances props={by("flower")} geometry={g.flower} colors={["#f5b5b5", "#f9d9a8", "#cdb4f6", "#ffffff"]} y={() => 0.15} quality={quality} />
          <Instances props={by("fence")} geometry={g.fence} color="#7a6450" y={() => 0.25} quality={quality} />
        </>
      )}
    </>
  );
}

/** The Foreman on the plaza: one batched mesh that turns (eased) to face the building the latest
 * event was about. `look` null = keep facing where it last looked. */
export function Foreman({ look }: { look?: [number, number] | null }) {
  const [fx, fz] = LAYOUT.plaza.foreman;
  const geo = useMemo(() => {
    const b = new Batch();
    robot(b, 0, 0, 0, 1.7, "#b9a3ff", true);
    return b.build();
  }, []);
  const ref = useRef<THREE.Group>(null);
  const want = useRef(0.3);
  if (look) want.current = Math.atan2(look[0] - fx, look[1] - fz);
  useFrame((_, dt) => {
    const g = ref.current;
    if (!g) return;
    // shortest way round, eased
    const d = Math.atan2(Math.sin(want.current - g.rotation.y), Math.cos(want.current - g.rotation.y));
    if (Math.abs(d) > 1e-3) g.rotation.y += d * Math.min(1, dt * 4);
  });
  return (
    <group ref={ref} position={[fx, 0, fz]} rotation={[0, 0.3, 0]}>
      <mesh geometry={geo} castShadow>
        <meshStandardMaterial vertexColors roughness={0.8} flatShading />
      </mesh>
    </group>
  );
}

/** Lamp bulbs, one instanced mesh. Dim while the town is being built; bright once it is done. */
function Lamps({ lit }: { lit: boolean }) {
  const lamps = useMemo(() => LAYOUT.props.filter((p) => p.kind === "lamp"), []);
  const ref = useRef<THREE.InstancedMesh>(null);
  const mat = useRef<THREE.MeshStandardMaterial>(null);
  const geo = useMemo(() => new THREE.SphereGeometry(0.17, 10, 7), []);
  useLayoutEffect(() => {
    const m = ref.current!, mx = new THREE.Matrix4();
    lamps.forEach((p, i) => m.setMatrixAt(i, mx.makeTranslation(p.x, 1.5, p.z)));
    m.instanceMatrix.needsUpdate = true;
  }, [lamps]);
  useFrame((_, dt) => {
    const m = mat.current;
    if (!m) return;
    const target = lit ? 2.4 : 0.9;
    if (Math.abs(m.emissiveIntensity - target) > 1e-3) m.emissiveIntensity += (target - m.emissiveIntensity) * Math.min(1, dt * 2);
  });
  return (
    <instancedMesh ref={ref} args={[geo, undefined, Math.max(1, lamps.length)]}>
      <meshStandardMaterial ref={mat} color={C.lamp} emissive={C.lamp} emissiveIntensity={0.9} toneMapped={false} />
    </instancedMesh>
  );
}

/** Painted chevrons along the ring road, pointing the way the stages flow. */
function Arrows() {
  const ref = useRef<THREE.InstancedMesh>(null);
  const items = useMemo(() => ROADS_SAMPLED.filter((r) => r.road.type === "ring").flatMap((r) => along(r.pts, 2.6, 1.4)), []);
  const geo = useMemo(() => {
    const s = new THREE.Shape();
    s.moveTo(0, 0.42); s.lineTo(0.42, -0.3); s.lineTo(0, -0.1); s.lineTo(-0.42, -0.3); s.closePath();
    const g = new THREE.ShapeGeometry(s);
    g.rotateX(-Math.PI / 2); // lie flat, pointing along +z
    return g;
  }, []);
  useLayoutEffect(() => {
    const m = ref.current!, mat = new THREE.Matrix4();
    items.forEach((a, i) => {
      mat.compose(new THREE.Vector3(a.x, 0.075, a.z), new THREE.Quaternion().setFromEuler(new THREE.Euler(0, a.heading, 0)), new THREE.Vector3(1, 1, 1));
      m.setMatrixAt(i, mat);
    });
    m.instanceMatrix.needsUpdate = true;
  }, [items]);
  return (
    <instancedMesh ref={ref} args={[geo, undefined, items.length]} receiveShadow>
      <meshStandardMaterial color={C.arrow} roughness={0.9} side={THREE.DoubleSide} />
    </instancedMesh>
  );
}

/** A boom barrier across a spoke, 1.5x the usual size. `closed` 0 = raised (open), 1 = lowered. */
export function Barrier({ x, z, rot, closed = 0 }: { x: number; z: number; rot: number; closed?: number }) {
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
    <group position={[x, 0.06, z]} rotation={[0, ang, 0]}>
      <mesh position={[0, 0.6, -1.5]} castShadow>
        <boxGeometry args={[0.55, 1.2, 0.55]} />
        <meshStandardMaterial color="#6b5f78" roughness={0.8} flatShading />
      </mesh>
      <mesh position={[0, 1.3, -1.5]}>
        <sphereGeometry args={[0.2, 8, 6]} />
        <meshStandardMaterial ref={lamp} color="#6cc788" emissive="#6cc788" emissiveIntensity={0.35} />
      </mesh>
      <group ref={arm} position={[0, 1.05, -1.5]} rotation={[-(Math.PI * 0.42) * (1 - closed), 0, 0]}>
        {[0, 1, 2].map((i) => (
          <mesh key={i} position={[0, 0, 0.35 + i * 0.7]} castShadow>
            <boxGeometry args={[0.2, 0.2, 0.7]} />
            <meshStandardMaterial color={i % 2 ? "#e8707a" : "#ffffff"} roughness={0.7} flatShading />
          </mesh>
        ))}
      </group>
    </group>
  );
}

export function TownStatic({ quality = "high", plan, circuitOpen = false, foremanLook = null, built = false }: {
  quality?: Quality;
  plan?: Plan;
  circuitOpen?: boolean;
  foremanLook?: [number, number] | null; // where the Foreman looks (the latest event's building)
  built?: boolean; // the goal succeeded: the lamps brighten
}) {
  // No plan given = the Phase A preview (happy-path layout with ghosts, labels and three idle workers).
  const preview = plan === undefined;
  const used = plan ?? LAYOUT.preview.zones;
  const { geo } = useStaticGeometry(used, preview);
  const { board, zones, plaza } = LAYOUT;
  const rim = useMemo(() => slab(board.w + 1.4, board.d + 1.4, board.corner + 0.5, 0.9), [board]);
  const top = useMemo(() => slab(board.w, board.d, board.corner, 0.5), [board]);
  return (
    <>
      <mesh geometry={rim} position={[0, -0.04, 0]} receiveShadow castShadow>
        <meshStandardMaterial color={C.boardSide} roughness={0.9} flatShading />
      </mesh>
      <mesh geometry={top} receiveShadow>
        <meshStandardMaterial color={C.grass} roughness={0.95} />
      </mesh>
      <mesh geometry={geo} receiveShadow castShadow>
        <meshStandardMaterial vertexColors roughness={0.85} flatShading />
      </mesh>
      <Arrows />
      <Foreman look={foremanLook} />
      <Lamps lit={built} />
      <Decoration quality={quality} />
      {LAYOUT.gates.map((g) => (
        <Barrier key={g.id} x={g.x} z={g.z} rot={g.rot} closed={circuitOpen ? 1 : 0} />
      ))}
      {/* labels: zone name with a step badge and a built counter; the Foreman */}
      {preview && LAYOUT.preview.zones.map((p) => {
        const z = zones[p.stage - 1];
        return (
          <Html key={z.id} position={[z.x, 3.0, z.z]} center zIndexRange={[10, 0]} className="label3d zone">
            <b className="badge" style={{ background: ZONE_COLORS[p.stage - 1] }}>{p.stage}</b>
            <strong>{p.name}</strong>
            <span className="count3d">0/{p.tasks} built</span>
          </Html>
        );
      })}
      <Html position={[plaza.foreman[0], 3.2, plaza.foreman[1] + 0.2]} center zIndexRange={[10, 0]} className="label3d small">
        <strong>Foreman</strong>
      </Html>
    </>
  );
}
