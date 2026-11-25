import { describe, expect, it } from "vitest";
import { AZIMUTH, ELEVATION, cameraPosition, fitDistance, zoomRange } from "./camera";

describe("camera fit", () => {
  it("fits the diagonal 40 x 30 board within a desktop viewport", () => {
    const d = fitDistance(40, 30, 952 / 545);
    expect(d).toBeGreaterThan(60);
    expect(d).toBeLessThan(75);
  });
  it("moves back on a narrow (phone) viewport and in on a wide one", () => {
    expect(fitDistance(40, 30, 0.6)).toBeGreaterThan(fitDistance(40, 30, 1.78));
    expect(fitDistance(40, 30, 3)).toBeLessThan(fitDistance(40, 30, 1.78) + 0.01);
  });
  it("the board really is inside the view at that distance (corners project within 85% of the width)", async () => {
    const { PerspectiveCamera, Vector3 } = await import("three");
    const aspect = 1.78;
    const d = fitDistance(40, 30, aspect);
    const cam = new PerspectiveCamera(35, aspect, 0.5, 400);
    const [x, y, z] = cameraPosition(d);
    cam.position.set(x, y, z);
    cam.lookAt(0, 0, 0);
    cam.updateMatrixWorld(true);
    for (const [sx, sz] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) {
      const p = new Vector3(sx * 20, -0.6, sz * 15).project(cam);
      expect(Math.abs(p.x)).toBeLessThanOrEqual(0.85 + 1e-3);
      expect(Math.abs(p.y)).toBeLessThanOrEqual(0.94 + 1e-3);
    }
  });
  it("keeps the zoom range relative to the fit", () => {
    const fit = fitDistance(40, 30, 1.78);
    const { min, max } = zoomRange(fit);
    expect(min / fit).toBeCloseTo(0.45);
    expect(max / fit).toBeCloseTo(1.15);
  });
  it("looks diagonally down without changing the distance", () => {
    const [x, y, z] = cameraPosition(40);
    expect(Math.atan2(x, z) * (180 / Math.PI)).toBeCloseTo(AZIMUTH);
    expect(Math.atan2(y, Math.hypot(x, z)) * (180 / Math.PI)).toBeCloseTo(ELEVATION);
    expect(Math.hypot(x, y, z)).toBeCloseTo(40);
  });
});
