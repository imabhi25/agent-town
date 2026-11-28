import { describe, expect, it } from "vitest";
import { LAYOUT } from "./town";
import { wallTarget, workSpot } from "./TownLive";

describe("wallTarget: how tall a building's walls are for its task", () => {
  it("follows the job's progress while it runs, with a visible stub at 0 and no roof before success", () => {
    expect(wallTarget({ status: "running", progress: 0 })).toBe(0.12);
    expect(wallTarget({ status: "running", progress: 0.5 })).toBe(0.5);
    expect(wallTarget({ status: "running", progress: 1 })).toBe(0.9); // only success tops it out
  });
  it("is full only when the task succeeded, and on the ground otherwise", () => {
    expect(wallTarget({ status: "succeeded", progress: 1 })).toBe(1);
    for (const status of ["planned", "queued", "retrying", "awaiting_approval", "dead"] as const) {
      expect(wallTarget({ status, progress: 0.75 })).toBe(0);
    }
  });
});

describe("workSpot: where a robot stands to build", () => {
  it("is outside the building, on the hub side, facing the building", () => {
    for (const [i, z] of LAYOUT.zones.entries()) {
      for (const slot of z.slots) {
        const p = workSpot(i + 1, slot);
        const fromSlot = Math.hypot(p.x - slot.x, p.z - slot.z);
        expect(fromSlot).toBeGreaterThan(slot.size / 2); // never inside the walls
        expect(Math.hypot(p.x - z.x, p.z - z.z)).toBeLessThan(Math.hypot(slot.x - z.x, slot.z - z.z));
        // facing: a step forward along the heading gets closer to the building
        const f = { x: p.x + Math.sin(p.face) * 0.1, z: p.z + Math.cos(p.face) * 0.1 };
        expect(Math.hypot(f.x - slot.x, f.z - slot.z)).toBeLessThan(fromSlot);
      }
    }
  });
});
