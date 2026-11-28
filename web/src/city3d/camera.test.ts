import { describe, expect, it } from "vitest";
import { AZIMUTH, cameraPosition, ELEVATION, fitDistance, zoomRange } from "./camera";

describe("camera fit", () => {
  it("fits the town at a sensible distance on a 16:9 map", () => {
    const d = fitDistance(27, 26, 1066 / 600);
    expect(d).toBeGreaterThan(25);
    expect(d).toBeLessThan(80);
  });
  it("moves back on a narrow (phone) viewport and in on a wide one", () => {
    expect(fitDistance(27, 26, 0.6)).toBeGreaterThan(fitDistance(27, 26, 1.78));
    expect(fitDistance(27, 26, 3)).toBeLessThan(fitDistance(27, 26, 1.78) + 0.01);
  });
  it("the area really is inside the view at that distance (corners within 85% of the width)", async () => {
    const { PerspectiveCamera, Vector3 } = await import("three");
    const aspect = 1.78;
    const d = fitDistance(27, 26, aspect);
    const cam = new PerspectiveCamera(35, aspect, 0.5, 400);
    const [x, y, z] = cameraPosition(d);
    cam.position.set(x, y, z);
    cam.lookAt(0, 0, 0);
    cam.updateMatrixWorld(true);
    for (const [sx, sz] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) {
      const p = new Vector3(sx * 13.5, -0.6, sz * 13).project(cam);
      expect(Math.abs(p.x)).toBeLessThanOrEqual(0.85 + 1e-3);
      expect(Math.abs(p.y)).toBeLessThanOrEqual(0.94 + 1e-3);
    }
  });
  it("keeps the zoom range relative to the fit", () => {
    const fit = fitDistance(27, 26, 1.78);
    const { min, max } = zoomRange(fit);
    expect(min / fit).toBeCloseTo(0.45);
    expect(max / fit).toBeCloseTo(1.15);
  });
  it("looks down from the south-west corner, so the board reads as a diamond", () => {
    const [x, y, z] = cameraPosition(40);
    expect(Math.hypot(x, y, z)).toBeCloseTo(40);
    expect(Math.atan2(y, Math.hypot(x, z)) * (180 / Math.PI)).toBeCloseTo(ELEVATION);
    expect(Math.atan2(-x, z) * (180 / Math.PI)).toBeCloseTo(AZIMUTH);
    expect(x).toBeLessThan(0);
    expect(z).toBeGreaterThan(0);
  });
});
