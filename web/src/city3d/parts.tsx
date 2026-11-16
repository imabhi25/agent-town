// Small reusable meshes. Everything is built from three.js primitives (no model files), flat
// shaded so the low-poly look comes from the geometry, in the same pastel palette as the 2D map.
import { useLayoutEffect, useRef } from "react";
import * as THREE from "three";
import { Html } from "@react-three/drei";
import { PALETTE } from "../palette";
import { ROADS, TREES } from "./layout";

export const hex = (n: number) => `#${n.toString(16).padStart(6, "0")}`;
const shade = (n: number, f: number) => `#${new THREE.Color(n).multiplyScalar(f).getHexString()}`;
export { shade };

type V3 = [number, number, number];
interface BoxProps {
  p: V3; // centre
  s: V3; // size
  c: string;
  cast?: boolean;
  receive?: boolean;
  rot?: V3;
}
export function Box({ p, s, c, cast = true, receive = true, rot }: BoxProps) {
  return (
    <mesh position={p} rotation={rot} castShadow={cast} receiveShadow={receive}>
      <boxGeometry args={s} />
      <meshStandardMaterial color={c} roughness={0.85} flatShading />
    </mesh>
  );
}

export function Label({ y, title, caption }: { y: number; title: string; caption?: string }) {
  return (
    <Html position={[0, y, 0]} center zIndexRange={[10, 0]} className="label3d">
      <strong>{title}</strong>
      {caption && <span>{caption}</span>}
    </Html>
  );
}

/** A district: a tinted plate under a building, like the coloured plots in the reference. */
export function Plate({ w, d, color }: { w: number; d: number; color: number }) {
  return (
    <>
      <Box p={[0, 0.06, 0]} s={[w + 1.2, 0.12, d + 1.2]} c={hex(color)} cast={false} />
      <Box p={[0, 0.13, 0]} s={[w + 0.7, 0.04, d + 0.7]} c={shade(color, 1.06)} cast={false} />
    </>
  );
}

export function Roads() {
  return (
    <>
      {ROADS.map(([x1, z1, x2, z2], i) => {
        const w = Math.abs(x2 - x1) || 0.9;
        const d = Math.abs(z2 - z1) || 0.9;
        return (
          <group key={i} position={[(x1 + x2) / 2, 0.1, (z1 + z2) / 2]}>
            <Box p={[0, 0, 0]} s={[w, 0.1, d]} c="#f3eee6" cast={false} />
            <Box p={[0, 0.06, 0]} s={[w > d ? w : 0.14, 0.02, w > d ? 0.14 : d]} c="#e4ddd0" cast={false} />
          </group>
        );
      })}
    </>
  );
}

/** Round pastel trees (sphere on a trunk), drawn as two InstancedMeshes: 2 draw calls in total. */
const TREE_COLORS = ["#8fd3c4", "#a8dca0", "#7cc7b8"];
export function Trees() {
  const crowns = useRef<THREE.InstancedMesh>(null);
  const trunks = useRef<THREE.InstancedMesh>(null);
  useLayoutEffect(() => {
    const m = new THREE.Matrix4();
    const col = new THREE.Color();
    TREES.forEach(([x, z, s, ci], i) => {
      m.compose(
        new THREE.Vector3(x, 1.05 * s, z),
        new THREE.Quaternion(),
        new THREE.Vector3(s, s, s),
      );
      crowns.current!.setMatrixAt(i, m);
      crowns.current!.setColorAt(i, col.set(TREE_COLORS[ci]));
      m.compose(
        new THREE.Vector3(x, 0.3 * s, z),
        new THREE.Quaternion(),
        new THREE.Vector3(s, s, s),
      );
      trunks.current!.setMatrixAt(i, m);
    });
    crowns.current!.instanceMatrix.needsUpdate = true;
    if (crowns.current!.instanceColor) crowns.current!.instanceColor.needsUpdate = true;
    trunks.current!.instanceMatrix.needsUpdate = true;
  }, []);
  return (
    <>
      <instancedMesh ref={crowns} args={[undefined, undefined, TREES.length]} castShadow receiveShadow>
        <icosahedronGeometry args={[0.62, 1]} />
        <meshStandardMaterial roughness={0.9} flatShading />
      </instancedMesh>
      <instancedMesh ref={trunks} args={[undefined, undefined, TREES.length]} castShadow>
        <cylinderGeometry args={[0.07, 0.1, 0.6, 6]} />
        <meshStandardMaterial color="#b89f86" roughness={0.9} flatShading />
      </instancedMesh>
    </>
  );
}

/** A small robot worker. The visor colour is the state (idle here; the others arrive in Phase B). */
export function Robot({ p, state = PALETTE.idle, rotY = 0 }: { p: V3; state?: number; rotY?: number }) {
  return (
    <group position={p} rotation={[0, rotY, 0]}>
      <Box p={[0, 0.42, 0]} s={[0.46, 0.42, 0.34]} c="#f4f1f8" />
      <Box p={[0, 0.82, 0]} s={[0.4, 0.32, 0.34]} c="#ffffff" />
      <Box p={[0, 0.83, 0.175]} s={[0.3, 0.14, 0.02]} c={hex(state)} cast={false} />
      <Box p={[0, 1.04, 0]} s={[0.04, 0.14, 0.04]} c="#b7b0c4" />
      <mesh position={[0, 1.13, 0]}>
        <sphereGeometry args={[0.055, 8, 6]} />
        <meshStandardMaterial color={hex(state)} roughness={0.6} flatShading />
      </mesh>
      <Box p={[-0.3, 0.42, 0]} s={[0.1, 0.34, 0.12]} c="#d9d3e3" />
      <Box p={[0.3, 0.42, 0]} s={[0.1, 0.34, 0.12]} c="#d9d3e3" />
      <Box p={[-0.12, 0.1, 0]} s={[0.14, 0.2, 0.16]} c="#b7b0c4" />
      <Box p={[0.12, 0.1, 0]} s={[0.14, 0.2, 0.16]} c="#b7b0c4" />
    </group>
  );
}
