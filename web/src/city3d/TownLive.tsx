// Phase B: the town driven by events. Takes the derived TownModel (townModel.ts) and draws one
// cottage per task, one robot per worker, the barriers and the zone labels. It decides nothing:
// every change comes from a model change, and motion (rising, walking, hammering) only eases
// toward what the model says.
//
// How a building tracks its task:
//   planned/queued  ghost foundation
//   running         wooden scaffold; the walls rise to the job's last job.progress (25/50/75%)
//   retrying        walls back to the ground, orange, with the retry countdown
//   succeeded       walls to full height, the roof drops on, the windows light up
//   dead            rubble and a red beacon
// So the last roof lands on the last task.succeeded, which is what completes the goal.
import { Html } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { C, ZONE_COLORS } from "./palette3d";
import { LAYOUT, type SlotSpec, zoneLabelAt } from "./town";
import type { BuildingStatus, TaskView, TownModel, WorkerView } from "./townModel";
import { Walker, targetNode } from "./walker";

/** Shared geometries: every cottage uses the same few, scaled by its slot size. */
const GEO = {
  box: new THREE.BoxGeometry(1, 1, 1),
  // a gable roof: a 3-sided prism along x with its ridge on top (apex at y = +1, base at y = -0.5)
  gable: new THREE.CylinderGeometry(1, 1, 1, 3, 1).rotateZ(Math.PI / 2).rotateX(-Math.PI / 2),
  spark: new THREE.OctahedronGeometry(0.09, 0),
};

const STATE_COLOR: Record<BuildingStatus, string> = {
  planned: "#b3ac98",
  queued: "#9aa6c8",
  running: "#7fb5ef",
  retrying: "#f0a35e",
  awaiting_approval: "#f2c14e",
  succeeded: "#6cc788",
  dead: "#e8707a",
};

const PITCH = 0.8; // roof height relative to a 60 degree gable: steep, like the mockup's cottages
const blend = (a: string, b: string, t: number) => `#${new THREE.Color(a).lerp(new THREE.Color(b), t).getHexString()}`;

/** "retry 2" with the seconds left when the clock agrees with the event time (live, or 1x replay). */
function RetryLabel({ task }: { task: TaskView }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(id);
  }, []);
  const left = task.retryAtMs === null ? null : (task.retryAtMs - now) / 1000;
  return <span>retry {task.attempt}{left !== null && left > 0 && left < 120 ? ` in ${left.toFixed(1)}s` : ""}</span>;
}

/** Eases `cur` toward `want` (exponential, frame-rate independent), snapping when close. */
const ease = (cur: number, want: number, dt: number, rate: number) =>
  Math.abs(want - cur) < 1e-3 ? want : cur + (want - cur) * Math.min(1, dt * rate);

/** How tall the walls should be (0..1) for a task. A running job shows at least a stub so the
 * start of work is visible before the first progress report. */
export function wallTarget(t: Pick<TaskView, "status" | "progress">): number {
  if (t.status === "succeeded") return 1;
  if (t.status === "running") return Math.max(0.12, Math.min(0.9, t.progress));
  return 0;
}

function Building({ task, slot, color }: { task: TaskView; slot: SlotSpec; color: string }) {
  const rot = (slot.rot * Math.PI) / 180;
  const walls = useRef<THREE.Group>(null);
  const roof = useRef<THREE.Group>(null);
  const scaffold = useRef<THREE.Group>(null);
  // one window material per building, so all its windows light up together
  const glass = useMemo(
    () => new THREE.MeshStandardMaterial({ color: C.windowOff, emissive: C.window, emissiveIntensity: 0, roughness: 0.4, toneMapped: false }),
    [],
  );
  useEffect(() => () => glass.dispose(), [glass]);
  // A building already built when the page opens does not animate.
  const built0 = task.status === "succeeded";
  const anim = useRef({ wall: built0 ? 1 : 0, roof: built0 ? 1 : 0, glow: built0 ? 1 : 0 });
  const s = slot.size;
  const w = s * 0.92; // wall footprint
  const h = s * 0.72; // wall height
  const wantWall = wallTarget(task);
  const done = task.status === "succeeded";
  useFrame((_, dt) => {
    const a = anim.current;
    a.wall = ease(a.wall, wantWall, dt, 3);
    // the roof waits for the walls to top out, then drops on; the windows light up after it
    a.roof = ease(a.roof, done && a.wall > 0.97 ? 1 : 0, dt, 5);
    a.glow = ease(a.glow, done && a.roof > 0.95 ? 1 : 0, dt, 2.5);
    if (walls.current) {
      walls.current.scale.y = Math.max(0.001, a.wall);
      walls.current.visible = a.wall > 0.01;
    }
    if (roof.current) {
      roof.current.visible = a.roof > 0.01;
      roof.current.position.y = h + 0.1 + (1 - a.roof) * 1.6;
      roof.current.scale.setScalar(0.6 + 0.4 * a.roof);
    }
    glass.emissiveIntensity = a.glow * 2.2;
    if (scaffold.current) scaffold.current.visible = task.status === "running" || task.status === "retrying";
  });
  const edge = STATE_COLOR[task.status];
  const e = s / 2 - 0.06;
  const wallColor = blend(C.wall, color, 0.12);
  const roofColor = blend(C.roof, color, 0.08);
  const rr = (w / 2 + 0.14) / 0.866; // gable radius so the eaves overhang the walls
  const post = h + 0.35;
  return (
    <group position={[slot.x, 0, slot.z]} rotation={[0, rot, 0]}>
      <mesh position={[0, 0.1, 0]} receiveShadow>
        <boxGeometry args={[s, 0.07, s]} />
        <meshStandardMaterial color={task.status === "dead" ? "#5b5262" : C.ghost} roughness={0.9} flatShading />
      </mesh>
      {/* a stone footing, there from the moment work starts */}
      {task.status !== "planned" && task.status !== "dead" && (
        <mesh geometry={GEO.box} position={[0, 0.2, 0]} scale={[w + 0.08, 0.14, w + 0.08]} receiveShadow>
          <meshStandardMaterial color={C.stone} roughness={0.95} flatShading />
        </mesh>
      )}
      {[[0, -e, s, 0.1], [0, e, s, 0.1], [-e, 0, 0.1, s], [e, 0, 0.1, s]].map(([x, z, ww, d], i) => (
        <mesh key={i} position={[x, 0.13, z]}>
          <boxGeometry args={[ww, 0.1, d]} />
          <meshStandardMaterial color={task.status === "planned" ? C.ghostEdge : edge} emissive={task.status === "planned" ? "#000000" : edge} emissiveIntensity={0.25} roughness={0.8} flatShading />
        </mesh>
      ))}
      {/* walls, door and windows: scaled from the ground up by the job's progress */}
      <group ref={walls} position={[0, 0.13, 0]} scale={[1, 0.001, 1]} visible={false}>
        <mesh geometry={GEO.box} position={[0, h / 2, 0]} scale={[w, h, w]} castShadow receiveShadow>
          <meshStandardMaterial color={wallColor} roughness={0.85} flatShading />
        </mesh>
        <mesh geometry={GEO.box} position={[0, h * 0.3, w / 2 + 0.01]} scale={[w * 0.2, h * 0.6, 0.03]}>
          <meshStandardMaterial color={C.door} roughness={0.9} />
        </mesh>
        {/* timber framing: corner posts and a beam under the eaves */}
        {[[-1, 1], [1, 1], [-1, -1], [1, -1]].map(([sx, sz], i) => (
          <mesh key={`b${i}`} geometry={GEO.box} position={[sx * (w / 2 - 0.02), h / 2, sz * (w / 2 - 0.02)]} scale={[0.07, h, 0.07]}>
            <meshStandardMaterial color={C.timberFrame} roughness={0.9} flatShading />
          </mesh>
        ))}
        <mesh geometry={GEO.box} position={[0, h - 0.04, 0]} scale={[w + 0.03, 0.07, w + 0.03]}>
          <meshStandardMaterial color={C.timberFrame} roughness={0.9} flatShading />
        </mesh>
        {[[-1, 1], [1, 1], [-1, -1], [1, -1], [0, -1]].map(([sx, sz], i) => (
          <mesh key={i} geometry={GEO.box} material={glass} position={[sx * w * 0.27, h * 0.58, sz * (w / 2 + 0.01)]} scale={[w * 0.22, h * 0.3, 0.03]} />
        ))}
      </group>
      {/* roof and chimney: drop on when the task succeeds */}
      <group ref={roof} position={[0, h + 1.7, 0]} visible={false}>
        <mesh geometry={GEO.gable} position={[0, rr * 0.5 * PITCH, 0]} scale={[w + 0.28, rr * PITCH, rr]} castShadow>
          <meshStandardMaterial color={roofColor} roughness={0.8} flatShading />
        </mesh>
        <mesh geometry={GEO.box} position={[w * 0.25, rr * PITCH, -w * 0.2]} scale={[0.17, 0.6, 0.17]} castShadow>
          <meshStandardMaterial color={C.stone} roughness={0.9} flatShading />
        </mesh>
        {/* a dormer window in the roof, lit with the others */}
        <mesh geometry={GEO.box} material={glass} position={[0, rr * PITCH * 0.35, rr * 0.67]} scale={[w * 0.22, h * 0.26, 0.03]} />
      </group>
      {/* wooden scaffold while a worker is on it (and while a retry waits) */}
      <group ref={scaffold} visible={false}>
        {[[-1, -1], [1, -1], [-1, 1], [1, 1]].map(([cx, cz], i) => (
          <mesh key={i} geometry={GEO.box} position={[cx * s * 0.44, post / 2 + 0.1, cz * s * 0.44]} scale={[0.07, post, 0.07]} castShadow>
            <meshStandardMaterial color={C.timber} roughness={0.9} flatShading />
          </mesh>
        ))}
        {[0.45, 0.9].flatMap((lv) =>
          [[0, -1, 1], [0, 1, 1], [-1, 0, 0], [1, 0, 0]].map(([cx, cz, alongX], i) => (
            <mesh
              key={`${lv}-${i}`}
              geometry={GEO.box}
              position={[cx * s * 0.44, post * lv + 0.1, cz * s * 0.44]}
              scale={alongX ? [s * 0.92, 0.05, 0.12] : [0.12, 0.05, s * 0.92]}
            >
              <meshStandardMaterial color={task.status === "retrying" ? edge : C.timber} roughness={0.9} flatShading />
            </mesh>
          )),
        )}
      </group>
      {task.status === "dead" && (
        <group>
          {[[-0.5, 0.3, 0.4], [0.4, 0.2, -0.2], [0.1, 0.25, 0.5]].map(([x, y, r], i) => (
            <mesh key={i} position={[x * s * 0.5, y, -r * s * 0.3]} rotation={[r, r * 2, r]} castShadow>
              <boxGeometry args={[s * 0.3, 0.3, s * 0.25]} />
              <meshStandardMaterial color="#7d7380" roughness={0.95} flatShading />
            </mesh>
          ))}
          <mesh position={[0, 1.7, 0]}>
            <sphereGeometry args={[0.22, 10, 8]} />
            <meshStandardMaterial color="#e8707a" emissive="#e8707a" emissiveIntensity={1.4} />
          </mesh>
          <mesh position={[0, 0.9, 0]}>
            <cylinderGeometry args={[0.05, 0.05, 1.6, 6]} />
            <meshStandardMaterial color="#8f8aa0" />
          </mesh>
        </group>
      )}
      {(task.status === "running" || task.status === "retrying" || task.status === "awaiting_approval" || task.status === "dead") && (
        <Html position={[0, h + 1.2, 0]} center zIndexRange={[10, 0]} className={`label3d small ${task.status}`}>
          {task.status === "running" ? (
            <span className="pct3d">{Math.round(task.progress * 100)}%</span>
          ) : task.status === "retrying" ? (
            <RetryLabel task={task} />
          ) : task.status === "dead" ? (
            <span>gave up</span>
          ) : (
            <span>needs approval</span>
          )}
        </Html>
      )}
    </group>
  );
}

/** Where a robot stands to work on a slot: just outside the building, on the side facing the zone's
 * hub (where the footpath comes in), so it is never inside the walls. */
export function workSpot(zone: number, slot: SlotSpec): { x: number; z: number; face: number } {
  const hub = LAYOUT.zones[zone - 1];
  const dx = hub.x - slot.x, dz = hub.z - slot.z;
  const l = Math.hypot(dx, dz) || 1;
  const r = slot.size / 2 + 0.38;
  const x = slot.x + (dx / l) * r, z = slot.z + (dz / l) * r;
  return { x, z, face: Math.atan2(slot.x - x, slot.z - z) };
}

/** A worker robot that walks its road route to wherever the model says it should be, then hammers
 * at its building while the task runs. */
function Worker({ view, model, order, onTap }: { view: WorkerView; model: TownModel; order: number; onTap?: (id: string) => void }) {
  const group = useRef<THREE.Group>(null);
  const arm = useRef<THREE.Group>(null);
  const spark = useRef<THREE.Mesh>(null);
  const walker = useRef<Walker | null>(null);
  const offset = useRef(0); // 0 = on the road node, 1 = at the work spot beside the building
  walker.current ??= new Walker(`pad${(order % LAYOUT.plaza.pads.length) + 1}`);
  const node = targetNode(model, view, order);
  const task = view.taskId ? model.tasks.find((t) => t.id === view.taskId) : undefined;
  const slot = task ? LAYOUT.zones[task.zone - 1]?.slots[task.slot] : undefined;
  const spot = useMemo(() => (task && slot ? workSpot(task.zone, slot) : null), [task?.zone, slot]); // eslint-disable-line react-hooks/exhaustive-deps
  const accent = task ? STATE_COLOR[task.status] : "#b7b0c4";
  const building = task?.status === "running";
  const phase = useMemo(() => (order * 0.37) % 1, [order]); // robots do not hammer in step
  useFrame((_, dt) => {
    const w = walker.current!;
    w.go(node);
    w.step(dt);
    const g = group.current;
    if (!g) return;
    const arrived = !w.moving && spot !== null && w.target === node;
    offset.current = ease(offset.current, arrived ? 1 : 0, dt, 6);
    const o = offset.current;
    const x = spot ? w.x + (spot.x - w.x) * o : w.x;
    const z = spot ? w.z + (spot.z - w.z) * o : w.z;
    g.position.set(x, w.moving ? Math.abs(Math.sin(performance.now() / 90)) * 0.07 : 0, z);
    if (w.moving) g.rotation.y = w.heading;
    else if (spot && o > 0.5) g.rotation.y = spot.face;
    // hammering: a quick down-stroke, a slower lift; a spark at the bottom of each stroke
    const working = building && arrived && o > 0.9;
    const t = ((performance.now() / 520 + phase) % 1);
    const swing = working ? (t < 0.25 ? t / 0.25 : 1 - (t - 0.25) / 0.75) : 0;
    if (arm.current) arm.current.rotation.x = -1.9 + swing * 1.9;
    if (spark.current) {
      const hit = working && t < 0.3 && t > 0.2;
      spark.current.visible = hit;
      if (hit) spark.current.rotation.y += dt * 20;
    }
  });
  return (
    <group
      ref={group}
      onClick={(e) => {
        e.stopPropagation();
        onTap?.(view.id); // click a robot to kill that worker (the controller refuses it on a recording)
      }}
      onPointerOver={() => onTap && (document.body.style.cursor = "pointer")}
      onPointerOut={() => (document.body.style.cursor = "")}
    >
      <mesh position={[0, 0.5, 0]} castShadow>
        <boxGeometry args={[0.55, 0.5, 0.4]} />
        <meshStandardMaterial color="#e6e1ee" roughness={0.8} flatShading />
      </mesh>
      <mesh position={[0, 0.98, 0]} castShadow>
        <boxGeometry args={[0.46, 0.38, 0.4]} />
        <meshStandardMaterial color="#f4f1f8" roughness={0.8} flatShading />
      </mesh>
      <mesh position={[0, 0.99, 0.21]}>
        <boxGeometry args={[0.34, 0.16, 0.02]} />
        <meshStandardMaterial color={accent} emissive={accent} emissiveIntensity={0.8} />
      </mesh>
      <mesh position={[0, 1.22, 0]} castShadow>
        <boxGeometry args={[0.5, 0.09, 0.44]} />
        <meshStandardMaterial color={C.hardhat} roughness={0.7} flatShading />
      </mesh>
      {/* right arm with a hammer, pivoting at the shoulder */}
      <group ref={arm} position={[0.33, 0.68, 0]} rotation={[-1.9, 0, 0]}>
        <mesh position={[0, -0.18, 0]}>
          <boxGeometry args={[0.1, 0.36, 0.1]} />
          <meshStandardMaterial color="#cfc8db" roughness={0.8} flatShading />
        </mesh>
        <mesh position={[0, -0.36, 0.14]}>
          <boxGeometry args={[0.05, 0.05, 0.3]} />
          <meshStandardMaterial color={C.timber} roughness={0.9} />
        </mesh>
        <mesh position={[0, -0.36, 0.3]}>
          <boxGeometry args={[0.16, 0.1, 0.1]} />
          <meshStandardMaterial color="#8d8a99" roughness={0.5} metalness={0.4} flatShading />
        </mesh>
      </group>
      <mesh ref={spark} geometry={GEO.spark} position={[0.33, 0.3, 0.45]} visible={false}>
        <meshStandardMaterial color="#ffe08a" emissive="#ffd060" emissiveIntensity={3} toneMapped={false} />
      </mesh>
    </group>
  );
}

export function TownLive({ model, onWorkerTap }: { model: TownModel; onWorkerTap?: (id: string) => void }) {
  const workers = useMemo(() => model.workers.filter((w) => !w.gone).sort((a, b) => a.id.localeCompare(b.id)), [model.workers]);
  return (
    <>
      {model.tasks.map((t) => {
        const slot = LAYOUT.zones[t.zone - 1].slots[t.slot];
        return slot ? <Building key={t.id} task={t} slot={slot} color={ZONE_COLORS[t.zone - 1]} /> : null;
      })}
      {workers.map((w, i) => (
        <Worker key={w.id} view={w} model={model} order={i} onTap={onWorkerTap} />
      ))}
      {model.goalStatus === "succeeded" && (
        <Html position={[LAYOUT.plaza.foreman[0], 4.4, LAYOUT.plaza.foreman[1]]} center zIndexRange={[10, 0]} className="label3d small done">
          <strong>Town built!</strong>
        </Html>
      )}
      {model.zones.map((z) => {
        const spec = LAYOUT.zones[z.zone - 1];
        const names = model.tasks.filter((t) => t.zone === z.zone).map((t) => t.name);
        return (
          <Html key={spec.id} position={zoneLabelAt(spec)} center zIndexRange={[10, 0]} className="label3d zone">
            <b className="badge" style={{ background: ZONE_COLORS[z.zone - 1] }}>{z.zone}</b>
            <strong>{names.length > 2 ? `${names[0]} +${names.length - 1}` : names.join(", ")}</strong>
            <span className="count3d">{z.built}/{z.tasks} built</span>
          </Html>
        );
      })}
    </>
  );
}
