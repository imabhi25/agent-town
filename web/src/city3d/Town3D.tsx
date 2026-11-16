// The part of the 3D city that moves. Everything here is driven by the headless Scene (which is
// fed ONLY by the event store) through `Poses`; nothing starts moving, failing or succeeding on its
// own. Per-frame work writes numbers onto objects that were created once: no allocation in the
// loop. Pools: 32 robots, 12 flying job tokens, instanced piles for queue/done/dead tokens.
import { useFrame, useThree } from "@react-three/fiber";
import { Html } from "@react-three/drei";
import { useEffect, useLayoutEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { PALETTE, type WorkerStateName } from "../palette";
import { MAX_WORKERS, Scene } from "../scene";
import type { StoreState } from "../store";
import { Buildings, type BuildingRefs } from "./Buildings";
import { GATE_POS, plot } from "./layout";
import { MAX_FLY, Poses } from "./poses";
import { Box, Robot, hex } from "./parts";
import { NODE } from "./routes";

export interface TownHooks {
  getState: () => StoreState;
  onChange: (scene: Scene) => void;
  onWorkerTap: (workerId: string) => void;
}

const ROAD_TOP = 0.16;
const ROBOT_SCALE = 1.6; // robots are small next to buildings; bigger reads better from above
const QUEUE_MAX = 12;
const DONE_MAX = 12;
const DLQ_MAX = 8;

function pileMatrices(count: number, x0: number, z0: number, cols: number, step: number): THREE.Matrix4[] {
  const q = new THREE.Quaternion();
  const s = new THREE.Vector3(1, 1, 1);
  return Array.from({ length: count }, (_, i) =>
    new THREE.Matrix4().compose(
      new THREE.Vector3(x0 + (i % cols) * step, 0.13 + 0.15, z0 + Math.floor(i / cols) * step),
      q,
      s,
    ),
  );
}

function Pile({ matrices, color, meshRef }: { matrices: THREE.Matrix4[]; color: string; meshRef: React.RefObject<THREE.InstancedMesh> }) {
  useLayoutEffect(() => {
    matrices.forEach((m, i) => meshRef.current!.setMatrixAt(i, m));
    meshRef.current!.instanceMatrix.needsUpdate = true;
    meshRef.current!.count = 0;
  }, [matrices, meshRef]);
  return (
    <instancedMesh ref={meshRef} args={[undefined, undefined, matrices.length]} castShadow>
      <boxGeometry args={[0.3, 0.3, 0.3]} />
      <meshStandardMaterial color={color} roughness={0.8} flatShading />
    </instancedMesh>
  );
}

const set = (el: HTMLElement | null, text: string) => {
  if (el && el.textContent !== text) el.textContent = text;
};

export function Town3D({ hooks }: { hooks: TownHooks }) {
  const scene = useMemo(() => new Scene(), []);
  const poses = useMemo(() => new Poses(), []);
  const gl = useThree((s) => s.gl);
  const camera = useThree((s) => s.camera);

  // materials, one per robot (its accent colour is its state), plus gate and approval colours
  const accents = useMemo(() => Array.from({ length: MAX_WORKERS }, () => new THREE.MeshStandardMaterial({ color: PALETTE.idle, roughness: 0.6, flatShading: true })), []);
  const gateMat = useMemo(() => new THREE.MeshStandardMaterial({ color: PALETTE.gateOpen, roughness: 0.6, flatShading: true }), []);
  const flyMat = useMemo(() => new THREE.MeshStandardMaterial({ color: PALETTE.busy, roughness: 0.6, flatShading: true }), []);
  const stateColors = useMemo(() => {
    const m = {} as Record<WorkerStateName, THREE.Color>;
    for (const k of ["idle", "busy", "retrying", "failed", "success"] as const) m[k] = new THREE.Color(PALETTE[k]);
    return m;
  }, []);
  const cOpen = useMemo(() => new THREE.Color(PALETTE.gateOpen), []);
  const cShut = useMemo(() => new THREE.Color(PALETTE.gateClosed), []);
  useEffect(() => () => [...accents, gateMat, flyMat].forEach((m) => m.dispose()), [accents, gateMat, flyMat]);

  const robotRefs = useRef<(THREE.Group | null)[]>([]);
  const carryRefs = useRef<(THREE.Mesh | null)[]>([]);
  const lastState = useRef<WorkerStateName[]>(Array(MAX_WORKERS).fill("idle"));
  const flyRefs = useRef<(THREE.Mesh | null)[]>([]);
  const gateArm = useRef<THREE.Group>(null);
  const gateText = useRef<HTMLSpanElement>(null);
  const approval = useRef<THREE.Group>(null);
  const approvalText = useRef<HTMLSpanElement>(null);
  const approvalScale = useRef(0);
  const refs: BuildingRefs = {
    planner: useRef<THREE.Group>(null),
    queue: useRef<THREE.Group>(null),
    queueCount: useRef<HTMLSpanElement>(null),
    doneCount: useRef<HTMLSpanElement>(null),
    dlqCount: useRef<HTMLSpanElement>(null),
  };
  const queuePile = useRef<THREE.InstancedMesh>(null);
  const donePile = useRef<THREE.InstancedMesh>(null);
  const dlqPile = useRef<THREE.InstancedMesh>(null);
  const q = plot("queue"), dn = plot("done"), dl = plot("dlq");
  const queueM = useMemo(() => pileMatrices(QUEUE_MAX, q.x - 1.5, q.z + q.d / 2 + 0.05, 6, 0.4), [q.x, q.z, q.d]);
  const doneM = useMemo(() => pileMatrices(DONE_MAX, dn.x - 1.0, dn.z + dn.d / 2 + 0.05, 6, 0.4), [dn.x, dn.z, dn.d]);
  const dlqM = useMemo(() => pileMatrices(DLQ_MAX, dl.x - 1.1, dl.z + dl.d / 2 + 0.05, 8, 0.3), [dl.x, dl.z, dl.d]);

  const last = useRef({ events: null as unknown, snapshot: null as unknown, version: -1 });
  const hooksRef = useRef(hooks);
  hooksRef.current = hooks;

  useFrame((_, delta) => {
    const dt = Math.min(delta, 0.1);
    const state = hooksRef.current.getState();
    const l = last.current;
    let changed = false;
    if (state.events !== l.events || state.snapshot !== l.snapshot) {
      l.events = state.events;
      l.snapshot = state.snapshot;
      scene.sync(state.snapshot, state.events); // the whole batch for this frame
      changed = true;
    }
    scene.step(dt);
    poses.update(scene, dt);

    for (let i = 0; i < MAX_WORKERS; i++) {
      const p = poses.list[i];
      const g = robotRefs.current[i];
      if (!g) continue;
      if (g.visible !== p.visible) g.visible = p.visible;
      if (!p.visible) continue;
      g.position.set(p.x, ROAD_TOP + p.bob, p.z);
      g.rotation.y = p.heading;
      g.scale.setScalar(p.scale * ROBOT_SCALE);
      if (lastState.current[i] !== p.state) {
        lastState.current[i] = p.state;
        accents[i].color.copy(stateColors[p.state]);
      }
      const c = carryRefs.current[i];
      if (c && c.visible !== p.carrying) c.visible = p.carrying;
    }
    for (let i = 0; i < MAX_FLY; i++) {
      const f = poses.fly[i];
      const m = flyRefs.current[i];
      if (!m) continue;
      if (m.visible !== f.active) m.visible = f.active;
      if (f.active) m.position.set(f.x, f.y, f.z);
    }

    // piles and counters
    if (queuePile.current) queuePile.current.count = Math.min(scene.queueDepth, QUEUE_MAX);
    if (donePile.current) donePile.current.count = Math.min(scene.done, DONE_MAX);
    if (dlqPile.current) dlqPile.current.count = Math.min(scene.dead, DLQ_MAX);
    set(refs.queueCount.current, `${scene.queueDepth} waiting`);
    set(refs.doneCount.current, `${scene.done} done`);
    set(refs.dlqCount.current, `${scene.dead} dead`);
    refs.planner.current?.scale.setScalar(1 + scene.plannerPulse * 0.05);
    refs.queue.current?.scale.setScalar(1 + scene.queuePulse * 0.05);

    // circuit gate: a boom barrier. Raised = open, lowered across the road = closed.
    if (gateArm.current) gateArm.current.rotation.x = (1 - scene.gate) * (Math.PI / 2);
    gateMat.color.copy(cOpen).lerp(cShut, scene.gate);
    set(gateText.current, scene.gate > 0.5 ? "circuit TRIPPED" : "circuit ok");

    // approval gate: visible only while a step is waiting for a click
    const want = scene.approvals.length > 0 ? 1 : 0;
    approvalScale.current += (want - approvalScale.current) * Math.min(1, dt * 10);
    if (Math.abs(want - approvalScale.current) < 0.01) approvalScale.current = want;
    if (approval.current) {
      approval.current.visible = approvalScale.current > 0;
      approval.current.scale.setScalar(Math.max(0.001, approvalScale.current));
    }
    set(approvalText.current, scene.approvals.length ? `approval needed (${scene.approvals.length})` : "");

    if (changed || scene.version !== l.version) {
      l.version = scene.version;
      hooksRef.current.onChange(scene);
    }
  });

  // Hooks for tests and the report: summary of the scene, and where a robot is on screen.
  useEffect(() => {
    const w = (window.__town3d ??= { fps: 0, frames: 0 });
    const v = new THREE.Vector3();
    w.summary = () => scene.summary();
    w.workerScreen = (id) => {
      const i = scene.pool.findIndex((x) => x.active && x.id === id);
      if (i < 0 || !poses.list[i].visible) return null;
      const p = poses.list[i];
      v.set(p.x, 0.9, p.z).project(camera);
      const r = gl.domElement.getBoundingClientRect();
      return { x: r.left + ((v.x + 1) / 2) * r.width, y: r.top + ((1 - v.y) / 2) * r.height };
    };
    return () => {
      delete w.summary;
      delete w.workerScreen;
    };
  }, [scene, poses, camera, gl]);

  const [gx, gz] = GATE_POS;
  const doneN = NODE.doneN;
  return (
    <>
      <Buildings refs={refs} />
      <Pile matrices={queueM} color="#e8b878" meshRef={queuePile} />
      <Pile matrices={doneM} color={hex(PALETTE.success)} meshRef={donePile} />
      <Pile matrices={dlqM} color={hex(PALETTE.failed)} meshRef={dlqPile} />

      {/* circuit-breaker gate on the road to Downstream */}
      <group position={[gx, 0.16, gz + 0.62]}>
        <Box p={[0, 0.3, 0]} s={[0.2, 0.6, 0.2]} c="#6b5f78" />
        <group ref={gateArm} position={[0, 0.5, 0]}>
          <mesh position={[0, 0, -0.62]} material={gateMat} castShadow>
            <boxGeometry args={[0.14, 0.14, 1.24]} />
          </mesh>
        </group>
        <Html position={[0, 1.4, -0.6]} center zIndexRange={[10, 0]} className="label3d small">
          <span ref={gateText} />
        </Html>
      </group>

      {/* approval gate: an arch across the road into Done, only while a step awaits approval */}
      <group ref={approval} position={[doneN[0], 0.16, (NODE.dsF[1] + doneN[1]) / 2]} visible={false}>
        <Box p={[-0.75, 0.55, 0]} s={[0.16, 1.1, 0.16]} c="#6b5f78" />
        <Box p={[0.75, 0.55, 0]} s={[0.16, 1.1, 0.16]} c="#6b5f78" />
        <Box p={[0, 1.1, 0]} s={[1.7, 0.2, 0.2]} c="#f2b95b" />
        <Box p={[0, 0.9, 0]} s={[0.3, 0.3, 0.14]} c="#e39a2b" />
        <Html position={[-1.9, 1.0, 0]} center zIndexRange={[10, 0]} className="label3d small">
          <span ref={approvalText} />
        </Html>
      </group>

      {/* flying job tokens: Queue -> the worker that took the job */}
      {Array.from({ length: MAX_FLY }, (_, i) => (
        <mesh key={i} ref={(el) => void (flyRefs.current[i] = el)} material={flyMat} visible={false} castShadow>
          <boxGeometry args={[0.28, 0.28, 0.28]} />
        </mesh>
      ))}

      {/* the worker pool */}
      {Array.from({ length: MAX_WORKERS }, (_, i) => (
        <group
          key={i}
          ref={(el) => void (robotRefs.current[i] = el)}
          visible={false}
          onClick={(e) => {
            e.stopPropagation();
            const w = scene.pool[i];
            if (w.active && w.dying === 0) hooksRef.current.onWorkerTap(w.id);
          }}
          onPointerOver={() => (gl.domElement.style.cursor = "pointer")}
          onPointerOut={() => (gl.domElement.style.cursor = "")}
        >
          <Robot accent={accents[i]} />
          <mesh ref={(el) => void (carryRefs.current[i] = el)} position={[0, 1.55, 0]} material={accents[i]} visible={false} castShadow>
            <boxGeometry args={[0.3, 0.3, 0.3]} />
          </mesh>
        </group>
      ))}
    </>
  );
}
