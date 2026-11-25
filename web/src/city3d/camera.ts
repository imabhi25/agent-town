import { PerspectiveCamera, Vector3 } from "three";

// Camera maths, pure so it is unit tested without WebGL.
export const FOV = 35; // vertical, degrees
export const ELEVATION = 39; // degrees above the horizon
export const AZIMUTH = 34; // diagonal view of the board, measured from +z

/**
 * Distance at which the whole board fits. The board is projected through the real camera (so the
 * perspective, which makes the near edge wider, is accounted for): its widest point takes `fill`
 * (85%) of the viewport width and it never takes more than 94% of the height. Found by bisection.
 */
export function fitDistance(boardW: number, boardD: number, aspect: number, fill = 0.85): number {
  const cam = new PerspectiveCamera(FOV, aspect, 0.5, 400);
  const corners = [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([sx, sz]) => new Vector3((sx * boardW) / 2, -0.6, (sz * boardD) / 2));
  const fits = (d: number) => {
    const [x, y, z] = cameraPosition(d);
    cam.position.set(x, y, z);
    cam.lookAt(0, 0, 0);
    cam.updateMatrixWorld(true);
    cam.updateProjectionMatrix();
    let mx = 0, my = 0;
    for (const c of corners) {
      const p = c.clone().project(cam);
      mx = Math.max(mx, Math.abs(p.x));
      my = Math.max(my, Math.abs(p.y));
    }
    return mx <= fill && my <= 0.94;
  };
  let lo = 8, hi = 300;
  for (let i = 0; i < 40; i++) {
    const mid = (lo + hi) / 2;
    if (fits(mid)) hi = mid;
    else lo = mid;
  }
  return hi;
}

/** Zoom limits are relative to the fit distance, so any screen size zooms the same amount. */
export const zoomRange = (fit: number) => ({ min: fit * 0.45, max: fit * 1.15 });

/** Camera position for a given distance, looking toward the center of the board. */
export function cameraPosition(distance: number): [number, number, number] {
  const e = (ELEVATION * Math.PI) / 180;
  const a = (AZIMUTH * Math.PI) / 180;
  const horizontal = Math.cos(e) * distance;
  return [Math.sin(a) * horizontal, Math.sin(e) * distance, Math.cos(a) * horizontal];
}
