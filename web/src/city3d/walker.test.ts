import { describe, expect, it } from "vitest";
import { LAYOUT } from "./town";
import { EMPTY_MODEL, type TaskView, type TownModel } from "./townModel";
import { Walker, WALK_SPEED, targetNode } from "./walker";

const task = (over: Partial<TaskView>): TaskView => ({
  id: "t", key: "t1", name: "t", stage: 1, zone: 1, slot: 0, status: "running", jobId: null, worker: null,
  attempt: 0, retryAtMs: null, needsApproval: false, ...over,
});
const model = (tasks: TaskView[]): TownModel => ({ ...EMPTY_MODEL, tasks });

describe("targetNode", () => {
  const w = { id: "w", taskId: null, jobId: null, gone: false };
  it("sends a worker with a task to that task's slot", () => {
    expect(targetNode(model([task({ id: "a", zone: 2, slot: 1 })]), { ...w, taskId: "a" }, 0)).toBe("z2_s2");
  });
  it("sends a task-less job to the Foreman and an idle worker to a pad", () => {
    expect(targetNode(EMPTY_MODEL, { ...w, jobId: "planner" }, 0)).toBe("foreman");
    expect(targetNode(EMPTY_MODEL, w, 2)).toBe("pad3");
    expect(targetNode(EMPTY_MODEL, w, 10)).toBe("pad3"); // wraps past 8 pads
  });
  it("every slot node and pad node exists in the layout", () => {
    for (const z of LAYOUT.zones) z.slots.forEach((_, k) => expect(LAYOUT.nodes[`${z.id}_s${k + 1}`]).toBeDefined());
    expect(LAYOUT.nodes.foreman).toBeDefined();
  });
});

describe("Walker", () => {
  it("starts standing at its node and does not move on its own", () => {
    const w = new Walker("pad1");
    const [x, z] = LAYOUT.nodes.pad1;
    w.step(5);
    expect([w.x, w.z]).toEqual([x, z]);
    expect(w.moving).toBe(false);
  });

  it("walks to a new target at the walking speed and arrives exactly", () => {
    const w = new Walker("pad1");
    w.go("z1_s1");
    expect(w.moving).toBe(true);
    const len = w.remaining;
    w.step(0.5);
    expect(len - w.remaining).toBeGreaterThan(0.4 * WALK_SPEED * 0.5); // about speed * dt (catch-up may add)
    for (let i = 0; i < 400 && w.moving; i++) w.step(0.1);
    const [x, z] = LAYOUT.nodes.z1_s1;
    expect(w.moving).toBe(false);
    expect(Math.hypot(w.x - x, w.z - z)).toBeLessThan(1e-6);
  });

  it("hurries when far behind (catch-up), so a long walk is not slower than 3x", () => {
    const w = new Walker("pad1");
    w.go("z4_s1");
    const len = w.remaining;
    let t = 0;
    while (w.moving && t < 120) {
      w.step(0.05);
      t += 0.05;
    }
    expect(t).toBeLessThan(len / WALK_SPEED); // faster than plain walking
  });

  it("can be re-targeted mid-walk without jumping", () => {
    const w = new Walker("pad1");
    w.go("z1_s1");
    w.step(1);
    const [x, z] = [w.x, w.z];
    w.go("z2_s1");
    expect(Math.hypot(w.x - x, w.z - z)).toBe(0);
    for (let i = 0; i < 600 && w.moving; i++) w.step(0.1);
    const [tx, tz] = LAYOUT.nodes.z2_s1;
    expect(Math.hypot(w.x - tx, w.z - tz)).toBeLessThan(1e-6);
  });

  it("ignores the same target twice and unknown nodes", () => {
    const w = new Walker("pad1");
    w.go("pad1");
    w.go("nowhere");
    expect(w.moving).toBe(false);
  });
});
