// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApprovalCards, ChaosPanel, DeadLetters } from "./components";
import type { UiState } from "./controller";
import { ModeBar, Playback, PresetPicker, PromptBox, StatusPill } from "./controls";

afterEach(cleanup);

const ui = (over: Partial<UiState> = {}): UiState => ({
  mode: "recorded", presets: [
    { preset: "a", name: "Happy path", description: "all good" },
    { preset: "b", name: "Retries", description: "flaky" },
  ],
  preset: "a", loading: false, error: "", notice: "", play: "playing", speed: 1, delivered: 3, total: 10,
  health: "waking", healthReason: "", liveOffReason: "The live backend is waking up.", ...over,
});
const actions = () => ({
  selectPreset: vi.fn(), goLive: vi.fn(), goRecorded: vi.fn(), play: vi.fn(), pause: vi.fn(),
  restart: vi.fn(), setSpeed: vi.fn(), submitPrompt: vi.fn().mockResolvedValue(null),
});

describe("status pill", () => {
  it.each([
    ["waking", "Connecting"],
    ["offline", "Backend offline"],
    ["connected", "Backend connected"],
  ] as const)("%s", (health, text) => {
    render(<StatusPill ui={ui({ health })} />);
    expect(screen.getByRole("status").textContent).toBe(text);
  });
});

describe("preset picker and playback", () => {
  it("has a button per preset, marks the current one, and picks another", () => {
    const a = actions();
    render(<PresetPicker ui={ui()} a={a} />);
    expect(screen.getByRole("button", { name: "Happy path" }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "Retries" }));
    expect(a.selectPreset).toHaveBeenCalledWith("b");
    expect(screen.getByText("all good")).toBeTruthy();
  });

  it("pause, restart and speed controls call the actions", () => {
    const a = actions();
    render(<Playback ui={ui()} a={a} />);
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    fireEvent.click(screen.getByRole("button", { name: "Restart" }));
    const speed = screen.getByRole("combobox", { name: "Playback speed" }) as HTMLSelectElement;
    expect(speed.value).toBe("1");
    fireEvent.change(speed, { target: { value: "4" } });
    expect(a.pause).toHaveBeenCalled();
    expect(a.restart).toHaveBeenCalled();
    expect(a.setSpeed).toHaveBeenCalledWith(4);
  });

  it("shows Play when paused and Replay when ended", () => {
    const a = actions();
    const { rerender } = render(<Playback ui={ui({ play: "paused" })} a={a} />);
    fireEvent.click(screen.getByRole("button", { name: "Play" }));
    expect(a.play).toHaveBeenCalled();
    rerender(<Playback ui={ui({ play: "ended" })} a={a} />);
    expect(screen.getByRole("button", { name: "Replay" })).toBeTruthy();
  });

  it("shows loading and error states", () => {
    const { rerender } = render(<PresetPicker ui={ui({ loading: true })} a={actions()} />);
    expect(screen.getByText(/Loading the trace/)).toBeTruthy();
    rerender(<PresetPicker ui={ui({ error: "Could not load this recorded trace." })} a={actions()} />);
    expect(screen.getByRole("alert").textContent).toMatch(/Could not load/);
  });

  it("shows an empty state, not a blank box, when there are no presets", () => {
    render(<PresetPicker ui={ui({ presets: [], preset: null })} a={actions()} />);
    expect(screen.getByText(/No recorded presets/)).toBeTruthy();
  });
});

describe("live gating", () => {
  it("Live is disabled with a reason until the backend is connected", () => {
    const a = actions();
    const { rerender } = render(<ModeBar ui={ui()} a={a} />);
    const live = screen.getByRole("button", { name: "Live" }) as HTMLButtonElement;
    expect(live.disabled).toBe(true);
    expect(live.title).toMatch(/not connected/);
    rerender(<ModeBar ui={ui({ health: "connected" })} a={a} />);
    fireEvent.click(screen.getByRole("button", { name: "Live" }));
    expect(a.goLive).toHaveBeenCalled();
  });

  it("the prompt box is disabled with the reason when live is off", () => {
    render(<PromptBox ui={ui()} a={actions()} />);
    expect((screen.getByRole("textbox") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Run goal" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("The live backend is waking up.")).toBeTruthy();
  });

  it("the prompt box submits when live is on", () => {
    const a = actions();
    render(<PromptBox ui={ui({ mode: "live", liveOffReason: "" })} a={a} />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "plan a trip" } });
    fireEvent.click(screen.getByRole("button", { name: "Run goal" }));
    expect(a.submitPrompt).toHaveBeenCalledWith("plan a trip");
  });

  it("chaos, DLQ replay and approval buttons are disabled with a tooltip on a recording", () => {
    const api = { killWorker: vi.fn(), replay: vi.fn(), approve: vi.fn() } as never;
    const off = "You are watching a recorded trace.";
    render(
      <>
        <ChaosPanel api={api} workerIds={["w1"]} floodMax={10} notify={() => {}} off={off} />
        <DeadLetters jobs={[{ jobId: "job-1234567", type: "task", error: "" }]} api={api} notify={() => {}} off={off} />
        <ApprovalCards approvals={[{ taskId: "t1", goalId: "g1", name: "Publish" }]} api={api} notify={() => {}} off={off} />
      </>,
    );
    for (const name of ["Kill worker", "Start worker", "Replay", "Approve", "Reject"]) {
      const b = screen.getByRole("button", { name: new RegExp(name) }) as HTMLButtonElement;
      expect(b.disabled, name).toBe(true);
    }
    fireEvent.click(screen.getByRole("button", { name: "Replay" }));
    expect((api as { replay: ReturnType<typeof vi.fn> }).replay).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Approve" }).title).toBe(off);
  });
});
