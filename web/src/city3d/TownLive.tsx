// Phase B: the town driven by events. Takes the derived TownModel (townModel.ts) and draws one
// building per task, one robot per worker, the barriers and the zone labels. It decides nothing:
// every change comes from a model change, and motion (rising, walking, swinging) only eases
// toward what the model says.
import { Html } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { C, ZONE_COLORS } from "./palette3d";
import { LAYOUT, type SlotSpec } from "./town";
import type { BuildingStatus, TaskView, TownModel, WorkerView } from "./townModel";
import { Walker, targetNode } from "./walker";

const STATE_COLOR: Record<BuildingStatus, string> = {
  planned: "#b3ac98",
  queued: "#9aa6c8",
  running: "#7fb5ef",
  retrying: "#f0a35e",
  awaiting_approval: "#f2c14e",
  succeeded: "#6cc788",
  dead: "#e8707a",
};

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

function Building({ task, slot, color }: { task: TaskView; slot: SlotSpec; color: string }) {
  const rot = (slot.rot * Math.PI) / 180;
  const body = useRef<THREE.Group>(null);
  const scaffold = useRef<THREE.Group>(null);
  const rise = useRef(task.status === "succeeded" ? 1 : 0); // a building already built when the page opens does not animate
  const want = task.status === "succeeded" ? 1 : 0;
  useFrame((_, dt) => {
    if (rise.current !== want) rise.current = Math.abs(want - rise.current) < 1e-3 ? want : rise.current + (want - rise.current) * Math.min(1, dt * 3);
    if (body.current) {
      body.current.scale.y = Math.max(0.001, rise.current);
      body.current.visible = rise.current > 0.01;
    }
    if (scaffold.current) {
      const on = task.status === "running" || task.status === "retrying";
      scaffold.current.visible = on;
      if (on) scaffold.current.scale.y = 0.85 + Math.sin(performance.now() / 260) * 0.08; // working
    }
  });
  const s = slot.size;
  const h = s * 0.9;
  const edge = STATE_COLOR[task.status];
  const e = s / 2 - 0.06;
  return (
    <group position={[slot.x, 0, slot.z]} rotation={[0, rot, 0]}>
      <mesh position={[0, 0.1, 0]} receiveShadow>
        <boxGeometry args={[s, 0.07, s]} />
        <meshStandardMaterial color={task.status === "dead" ? "#cfc8c8" : C.ghost} roughness={0.9} flatShading />
      </mesh>
      {[[0, -e, s, 0.1], [0, e, s, 0.1], [-e, 0, 0.1, s], [e, 0, 0.1, s]].map(([x, z, w, d], i) => (
        <mesh key={i} position={[x, 0.13, z]}>
          <boxGeometry args={[w, 0.1, d]} />
          <meshStandardMaterial color={task.status === "planned" ? C.ghostEdge : edge} roughness={0.8} flatShading />
        </mesh>
      ))}
      <group ref={body} scale={[1, 0.001, 1]} visible={false}>
        <mesh position={[0, h / 2 + 0.1, 0]} castShadow receiveShadow>
          <boxGeometry args={[s * 0.82, h, s * 0.82]} />
          <meshStandardMaterial color={blend(C.wall, color, 0.35)} roughness={0.85} flatShading />
        </mesh>
        <mesh position={[0, h + 0.1 + s * 0.2, 0]} rotation={[0, Math.PI / 4, 0]} castShadow>
          <coneGeometry args={[s * 0.68, s * 0.4, 4]} />
          <meshStandardMaterial color={color} roughness={0.8} flatShading />
        </mesh>
      </group>
      <group ref={scaffold} visible={false}>
        {[[-1, -1], [1, -1], [-1, 1], [1, 1]].map(([cx, cz], i) => (
          <mesh key={i} position={[cx * s * 0.4, h * 0.45, cz * s * 0.4]} castShadow>
            <boxGeometry args={[0.1, h * 0.9, 0.1]} />
            <meshStandardMaterial color={edge} roughness={0.7} flatShading />
          </mesh>
        ))}
        <mesh position={[0, h * 0.45, 0]}>
          <boxGeometry args={[s * 0.7, h * 0.5, s * 0.7]} />
          <meshStandardMaterial color={edge} transparent opacity={0.28} roughness={0.7} />
        </mesh>
      </group>
      {task.status === "dead" && (
        <group>
          {[[-0.5, 0.3, 0.4], [0.4, 0.2, -0.2], [0.1, 0.25, 0.5]].map(([x, y, r], i) => (
            <mesh key={i} position={[x * s * 0.5, y, -r * s * 0.3]} rotation={[r, r * 2, r]} castShadow>
              <boxGeometry args={[s * 0.3, 0.3, s * 0.25]} />
              <meshStandardMaterial color="#9a909c" roughness={0.95} flatShading />
            </mesh>
          ))}
          <mesh position={[0, 1.7, 0]}>
            <sphereGeometry args={[0.22, 10, 8]} />
            <meshStandardMaterial color="#e8707a" emissive="#e8707a" emissiveIntensity={0.9} />
          </mesh>
          <mesh position={[0, 0.9, 0]}>
            <cylinderGeometry args={[0.05, 0.05, 1.6, 6]} />
            <meshStandardMaterial color="#8f8aa0" />
          </mesh>
        </group>
      )}
      {(task.status === "retrying" || task.status === "awaiting_approval" || task.status === "dead") && (
        <Html position={[0, h + 1.1, 0]} center zIndexRange={[10, 0]} className={`label3d small ${task.status}`}>
          {task.status === "retrying" ? <RetryLabel task={task} /> : task.status === "dead" ? <span>gave up</span> : <span>needs approval</span>}
        </Html>
      )}
    </group>
  );
}

/** A worker robot that walks its road route to wherever the model says it should be. */
function Worker({ view, model, order }: { view: WorkerView; model: TownModel; order: number }) {
  const group = useRef<THREE.Group>(null);
  const walker = useRef<Walker | null>(null);
  walker.current ??= new Walker(`pad${(order % LAYOUT.plaza.pads.length) + 1}`);
  const node = targetNode(model, view, order);
  const task = view.taskId ? model.tasks.find((t) => t.id === view.taskId) : undefined;
  const accent = task ? STATE_COLOR[task.status] : "#b7b0c4";
  useFrame((_, dt) => {
    const w = walker.current!;
    w.go(node);
    w.step(dt);
    const g = group.current;
    if (!g) return;
    g.position.set(w.x, w.moving ? Math.abs(Math.sin(performance.now() / 90)) * 0.07 : 0, w.z);
    if (w.moving) g.rotation.y = w.heading; // standing still keeps the last heading
  });
  return (
    <group ref={group}>
      <mesh position={[0, 0.5, 0]} castShadow>
        <boxGeometry args={[0.55, 0.5, 0.4]} />
        <meshStandardMaterial color="#f4f1f8" roughness={0.8} flatShading />
      </mesh>
      <mesh position={[0, 0.98, 0]} castShadow>
        <boxGeometry args={[0.46, 0.38, 0.4]} />
        <meshStandardMaterial color="#ffffff" roughness={0.8} flatShading />
      </mesh>
      <mesh position={[0, 0.99, 0.21]}>
        <boxGeometry args={[0.34, 0.16, 0.02]} />
        <meshStandardMaterial color={accent} emissive={accent} emissiveIntensity={0.4} />
      </mesh>
      <mesh position={[0, 1.3, 0]}>
        <sphereGeometry args={[0.08, 8, 6]} />
        <meshStandardMaterial color={accent} emissive={accent} emissiveIntensity={0.5} />
      </mesh>
    </group>
  );
}

export function TownLive({ model }: { model: TownModel }) {
  const workers = useMemo(() => model.workers.filter((w) => !w.gone).sort((a, b) => a.id.localeCompare(b.id)), [model.workers]);
  return (
    <>
      {model.tasks.map((t) => {
        const slot = LAYOUT.zones[t.zone - 1].slots[t.slot];
        return slot ? <Building key={t.id} task={t} slot={slot} color={ZONE_COLORS[t.zone - 1]} /> : null;
      })}
      {workers.map((w, i) => (
        <Worker key={w.id} view={w} model={model} order={i} />
      ))}
      {model.zones.map((z) => {
        const spec = LAYOUT.zones[z.zone - 1];
        const names = model.tasks.filter((t) => t.zone === z.zone).map((t) => t.name);
        return (
          <Html key={spec.id} position={[spec.x, 3.0, spec.z]} center zIndexRange={[10, 0]} className="label3d zone">
            <b className="badge" style={{ background: ZONE_COLORS[z.zone - 1] }}>{z.zone}</b>
            <strong>{names.length > 2 ? `${names[0]} +${names.length - 1}` : names.join(", ")}</strong>
            <span className="count3d">{z.built}/{z.tasks} built</span>
          </Html>
        );
      })}
    </>
  );
}
