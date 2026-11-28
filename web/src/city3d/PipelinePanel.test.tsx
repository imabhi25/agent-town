// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { formatElapsed, PipelinePanel } from "./PipelinePanel";
import { EMPTY_MODEL, type TaskView, type TownModel } from "./townModel";

afterEach(cleanup);

const task = (over: Partial<TaskView>): TaskView => ({
  id: "t", key: "t1", name: "Research", stage: 1, zone: 1, slot: 0, status: "planned", jobId: null, worker: null,
  attempt: 0, retryAtMs: null, needsApproval: false, progress: 0, ...over,
});
const model = (tasks: TaskView[], over: Partial<TownModel> = {}): TownModel => ({
  ...EMPTY_MODEL, tasks, zones: [{ zone: 1, tasks: tasks.length, built: tasks.filter((t) => t.status === "succeeded").length }], ...over,
});

describe("formatElapsed", () => {
  it("shows seconds, then minutes", () => {
    expect(formatElapsed(12_340)).toBe("12.3s");
    expect(formatElapsed(125_000)).toBe("2m 5s");
    expect(formatElapsed(-5)).toBe("0.0s");
  });
});

describe("PipelinePanel", () => {
  it("renders nothing before a plan exists", () => {
    const { container } = render(<PipelinePanel model={EMPTY_MODEL} />);
    expect(container.firstChild).toBeNull();
  });
  it("lists each task with its state and the retry attempt", () => {
    render(<PipelinePanel model={model([task({ status: "retrying", attempt: 2 })])} />);
    expect(screen.getByText("retrying (attempt 2)")).toBeTruthy();
    expect(screen.queryByRole("status")).toBeNull(); // no banner until the goal succeeds
  });
  it("shows the completion banner with time, tasks and retries", () => {
    render(
      <PipelinePanel
        model={model([task({ status: "succeeded" }), task({ id: "u", status: "succeeded" })], { goalStatus: "succeeded", startedMs: 0, finishedMs: 9000, retries: 1 })}
      />,
    );
    expect(screen.getByRole("status").textContent).toBe("Town built in 9.0s: 2 tasks, 1 retry");
  });
});
