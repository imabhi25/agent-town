// The 3D city. A real three.js scene: perspective camera, hemisphere + sun
// light, soft shadow maps, low-poly meshes. It is lazy-loaded (see CityMap) so three.js is not part
// of the first paint. It reads the same event store as the 2D map (through Town3D).
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { OrbitControls } from "@react-three/drei";
import { useRef } from "react";
import * as THREE from "three";
import { PALETTE } from "../palette";
import { Town3D, type TownHooks } from "./Town3D";
import { SLAB } from "./layout";
import { Box, Roads, Trees, hex } from "./parts";

declare global {
  interface Window {
    __town3d?: {
      fps: number;
      frames: number;
      summary?: () => unknown;
      workerScreen?: (id: string) => { x: number; y: number } | null; // for tests: where a robot is on screen
    };
  }
}

/** Counts frames and publishes a once-a-second fps figure (for tests and the report). */
function FpsProbe() {
  const acc = useRef({ t: 0, n: 0 });
  useFrame((_, dt) => {
    const a = acc.current;
    a.t += dt;
    a.n++;
    const w = (window.__town3d ??= { fps: 0, frames: 0 });
    w.frames++;
    if (a.t >= 1) {
      w.fps = Math.round(a.n / a.t);
      a.t = 0;
      a.n = 0;
    }
  });
  return null;
}

const PAN_LIMIT = { x: 6, zMin: -3, zMax: 4 };

function Controls() {
  const { invalidate } = useThree();
  return (
    <OrbitControls
      makeDefault
      target={[0, 0, 1]}
      enableDamping
      dampingFactor={0.12}
      minDistance={14}
      maxDistance={40}
      minPolarAngle={0.5} // never straight down...
      maxPolarAngle={1.15} // ...and never below the horizon: the camera stays "from above"
      minAzimuthAngle={-0.6}
      maxAzimuthAngle={0.6}
      screenSpacePanning={false}
      onChange={(e) => {
        const c = e?.target as { target: THREE.Vector3 } | undefined;
        if (!c) return;
        c.target.x = THREE.MathUtils.clamp(c.target.x, -PAN_LIMIT.x, PAN_LIMIT.x);
        c.target.z = THREE.MathUtils.clamp(c.target.z, PAN_LIMIT.zMin, PAN_LIMIT.zMax);
        c.target.y = 0;
        invalidate();
      }}
    />
  );
}

export default function Scene3D({ hooks }: { hooks: TownHooks }) {
  return (
    <Canvas
      shadows="soft"
      flat // no tone mapping: pastel colours stay as authored
      dpr={[1, 2]} // never more than 2x pixel density
      camera={{ fov: 32, near: 0.5, far: 120, position: [0, 17.5, 21] }}
      gl={{ antialias: true, powerPreference: "high-performance" }}
      data-testid="canvas3d"
    >
      <color attach="background" args={[hex(PALETTE.bg)]} />
      <fog attach="fog" args={[hex(PALETTE.bg), 45, 80]} />
      <hemisphereLight args={["#fff8f0", "#e4dcef", 1.55]} />
      <directionalLight
        position={[9, 16, 8]}
        intensity={1.9}
        color="#fff3e0"
        castShadow
        shadow-mapSize={[2048, 2048]}
        shadow-bias={-0.0004}
        shadow-radius={5}
        shadow-camera-left={-19}
        shadow-camera-right={19}
        shadow-camera-top={13}
        shadow-camera-bottom={-13}
        shadow-camera-near={1}
        shadow-camera-far={50}
      />
      {/* The floating slab: a grey rim under a pale top, like the reference's town plate. */}
      <Box p={[0, -0.55, 0]} s={[SLAB.w + 0.8, 0.5, SLAB.d + 0.8]} c="#c9c1cf" />
      <Box p={[0, -0.15, 0]} s={[SLAB.w, 0.3, SLAB.d]} c="#efe8da" cast={false} />
      <Roads />
      <Town3D hooks={hooks} />
      <Trees />
      <Controls />
      <FpsProbe />
    </Canvas>
  );
}
