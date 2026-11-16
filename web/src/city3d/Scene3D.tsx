// The 3D town: a perspective camera at 45 degrees fitted to the board, hemisphere + sun light with a
// 2048 soft shadow map, ACES tone mapping, an orbit camera with relative zoom limits. Lazy-loaded
// (see CityMap) so three.js is not part of the first paint. Phase A: the static town.
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { OrbitControls } from "@react-three/drei";
import { useEffect, useRef } from "react";
import * as THREE from "three";
import { cameraPosition, FOV, fitDistance, zoomRange } from "./camera";
import { C } from "./palette3d";
import { LAYOUT } from "./town";
import { TownStatic, type Quality } from "./TownStatic";

declare global {
  interface Window {
    __town3d?: {
      fps: number;
      frames: number;
      calls?: number;
      triangles?: number;
      summary?: () => unknown;
    };
  }
}

/** Counts frames and publishes fps, draw calls and triangles once a second (tests and the report). */
function Probe() {
  const acc = useRef({ t: 0, n: 0 });
  useFrame(({ gl }, dt) => {
    const a = acc.current;
    a.t += dt;
    a.n++;
    const w = (window.__town3d ??= { fps: 0, frames: 0 });
    w.frames++;
    if (a.t >= 1) {
      w.fps = Math.round(a.n / a.t);
      w.calls = gl.info.render.calls;
      w.triangles = gl.info.render.triangles;
      a.t = 0;
      a.n = 0;
    }
  });
  return null;
}

/** Keeps the whole board in view: the default distance is recomputed from the viewport on resize,
 * and the zoom limits stay relative to it. A user who has zoomed keeps their zoom ratio. */
function Rig() {
  const { camera, size, controls } = useThree();
  const last = useRef<number | null>(null);
  useEffect(() => {
    const fit = fitDistance(LAYOUT.board.w, LAYOUT.board.d, size.width / Math.max(1, size.height));
    const ratio = last.current ? camera.position.length() / last.current : 1;
    const [x, y, z] = cameraPosition(fit * ratio);
    camera.position.set(x, y, z);
    camera.lookAt(0, 0, 0);
    const c = controls as unknown as { minDistance: number; maxDistance: number; target: THREE.Vector3; update: () => void } | null;
    if (c) {
      const r = zoomRange(fit);
      c.minDistance = r.min;
      c.maxDistance = r.max;
      c.update();
    }
    last.current = fit;
  }, [camera, size.width, size.height, controls]);
  return null;
}

const PAN = { x: 8, z: 6 };

export default function Scene3D({ quality = "high" }: { quality?: Quality }) {
  return (
    <Canvas
      shadows="soft"
      dpr={[1, 2]} // never more than 2x pixel density
      camera={{ fov: FOV, near: 0.5, far: 220, position: cameraPosition(48) }}
      gl={{ antialias: true, powerPreference: "high-performance", toneMapping: THREE.ACESFilmicToneMapping, toneMappingExposure: 1.15 }}
      data-testid="canvas3d"
    >
      <color attach="background" args={[C.bg]} />
      <fog attach="fog" args={[C.bg, 90, 160]} />
      <hemisphereLight args={["#fff8f0", "#d9d3e8", 1.5]} />
      <directionalLight
        position={[14, 26, 12]}
        intensity={2.1}
        color="#fff3e0"
        castShadow={quality === "high"}
        shadow-mapSize={[2048, 2048]}
        shadow-bias={-0.0004}
        shadow-radius={4}
        shadow-camera-left={-26}
        shadow-camera-right={26}
        shadow-camera-top={20}
        shadow-camera-bottom={-20}
        shadow-camera-near={1}
        shadow-camera-far={80}
      />
      <TownStatic quality={quality} />
      <OrbitControls
        makeDefault
        enableDamping
        dampingFactor={0.12}
        minPolarAngle={0.45}
        maxPolarAngle={1.1}
        minAzimuthAngle={-0.5}
        maxAzimuthAngle={0.5}
        screenSpacePanning={false}
        onChange={(e) => {
          const c = e?.target as { target: THREE.Vector3 } | undefined;
          if (!c) return;
          c.target.x = THREE.MathUtils.clamp(c.target.x, -PAN.x, PAN.x);
          c.target.z = THREE.MathUtils.clamp(c.target.z, -PAN.z, PAN.z);
          c.target.y = 0;
        }}
      />
      <Rig />
      <Probe />
    </Canvas>
  );
}
