// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Api } from "./api";
import { ApprovalCards, ChaosPanel, DeadLetters, Legend } from "./components";

afterEach(cleanup);

const ok = { ok: true, status: 200, message: "" };
const fakeApi = (): Api & Record<keyof Api, ReturnType<typeof vi.fn>> =>
  ({
    killWorker: vi.fn().mockResolvedValue(ok),
    startWorker: vi.fn().mockResolvedValue(ok),
    setFailureRate: vi.fn().mockResolvedValue(ok),
    flood: vi.fn().mockResolvedValue(ok),
    replay: vi.fn().mockResolvedValue(ok),
    approve: vi.fn().mockResolvedValue(ok),
    reject: vi.fn().mockResolvedValue(ok),
    submitGoal: vi.fn().mockResolvedValue(ok),
  }) as never;

describe("ChaosPanel", () => {
  it("kills a random worker by default and the chosen one when selected", async () => {
    const api = fakeApi();
    render(<ChaosPanel api={api} workerIds={["worker-1", "worker-2"]} floodMax={100} notify={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Kill worker" }));
    await waitFor(() => expect(api.killWorker).toHaveBeenLastCalledWith(undefined));
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "worker-2" } });
    fireEvent.click(screen.getByRole("button", { name: "Kill worker" }));
    await waitFor(() => expect(api.killWorker).toHaveBeenLastCalledWith("worker-2"));
  });
  it("starts a worker", async () => {
    const api = fakeApi();
    render(<ChaosPanel api={api} workerIds={[]} floodMax={100} notify={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Start worker" }));
    await waitFor(() => expect(api.startWorker).toHaveBeenCalled());
  });
  it("sends the failure rate when the slider is released", async () => {
    const api = fakeApi();
    render(<ChaosPanel api={api} workerIds={[]} floodMax={100} notify={() => {}} />);
    const slider = screen.getByRole("slider");
    fireEvent.change(slider, { target: { value: "0.4" } });
    expect(screen.getByText("40%")).toBeTruthy();
    expect(api.setFailureRate).not.toHaveBeenCalled(); // not on every drag step
    fireEvent.pointerUp(slider);
    await waitFor(() => expect(api.setFailureRate).toHaveBeenCalledWith(0.4));
  });
  it("moves the slider when the server reports a new failure rate", () => {
    const props = { api: fakeApi(), workerIds: [], floodMax: 100, notify: () => {} };
    const { rerender } = render(<ChaosPanel {...props} serverRate={null} />);
    expect(screen.getByText("0%")).toBeTruthy();
    rerender(<ChaosPanel {...props} serverRate={0.6} />);
    expect(screen.getByText("60%")).toBeTruthy();
    expect((screen.getByRole("slider") as HTMLInputElement).value).toBe("0.6");
  });
  it("floods the chosen count and never more than the cap", async () => {
    const api = fakeApi();
    render(<ChaosPanel api={api} workerIds={[]} floodMax={100} notify={() => {}} />);
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "5000" } });
    fireEvent.click(screen.getByRole("button", { name: /Flood 100 mock jobs/ }));
    await waitFor(() => expect(api.flood).toHaveBeenCalledWith(100));
  });
  it("starts with a default of 50, or the cap if lower", () => {
    render(<ChaosPanel api={fakeApi()} workerIds={[]} floodMax={20} notify={() => {}} />);
    expect(screen.getByRole("button", { name: /Flood 20 mock jobs/ })).toBeTruthy();
  });
  it("reports a failed call (e.g. a 429) through notify", async () => {
    const api = fakeApi();
    const denied = { ok: false, status: 429, message: "Too many chaos requests." };
    api.flood.mockResolvedValue(denied);
    const notify = vi.fn();
    render(<ChaosPanel api={api} workerIds={[]} floodMax={100} notify={notify} />);
    fireEvent.click(screen.getByRole("button", { name: /Flood/ }));
    await waitFor(() => expect(notify).toHaveBeenCalledWith(denied));
  });
});

describe("DeadLetters", () => {
  it("shows an empty state", () => {
    render(<DeadLetters jobs={[]} api={fakeApi()} notify={() => {}} />);
    expect(screen.getByText(/Nothing dead/)).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });
  it("lists dead jobs, each with its own Replay button", async () => {
    const api = fakeApi();
    render(<DeadLetters jobs={[{ jobId: "aaaaaaaa-1", type: "llm.mock" }, { jobId: "bbbbbbbb-2" }]} api={api} notify={() => {}} />);
    const buttons = screen.getAllByRole("button", { name: "Replay" });
    expect(buttons).toHaveLength(2);
    fireEvent.click(buttons[1]);
    await waitFor(() => expect(api.replay).toHaveBeenCalledWith("bbbbbbbb-2"));
  });
});

describe("ApprovalCards", () => {
  it("renders nothing when no approval is pending", () => {
    const { container } = render(<ApprovalCards approvals={[]} api={fakeApi()} notify={() => {}} />);
    expect(container.innerHTML).toBe("");
  });
  it("approves and rejects the right task", async () => {
    const api = fakeApi();
    render(
      <ApprovalCards
        approvals={[{ taskId: "t1", goalId: "g", name: "Book the hotel" }, { taskId: "t2", goalId: "g", name: "Send the email" }]}
        api={api}
        notify={() => {}}
      />,
    );
    expect(screen.getByText("Book the hotel")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "Approve" })[0]);
    fireEvent.click(screen.getAllByRole("button", { name: "Reject" })[1]);
    await waitFor(() => expect(api.approve).toHaveBeenCalledWith("t1"));
    await waitFor(() => expect(api.reject).toHaveBeenCalledWith("t2"));
  });
});

describe("Legend", () => {
  it("is a polite live region that reads the scene as a sentence", () => {
    render(<Legend summary={{ workers: 4, busy: 2, queue: 7, done: 9, dead: 1, circuit: "tripped", approvals: 2 }} />);
    const el = screen.getByRole("status");
    expect(el.getAttribute("aria-live")).toBe("polite");
    expect(el.textContent).toBe("4 workers (2 busy) · 7 queued · 9 done · 1 dead · circuit tripped · 2 awaiting approval");
  });
});
