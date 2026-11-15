// The bar above the map: status pill, preset buttons, playback controls and the prompt box.
import { useState } from "react";
import { PILL } from "./health";
import { SPEEDS } from "./replay";
import type { UiState } from "./controller";

interface Actions {
  selectPreset: (id: string) => void;
  goLive: () => void;
  goRecorded: () => void;
  play: () => void;
  pause: () => void;
  restart: () => void;
  setSpeed: (s: number) => void;
  submitPrompt: (text: string) => Promise<unknown>;
}

export function StatusPill({ ui }: { ui: UiState }) {
  return (
    <span className={`pill ${ui.health}`} role="status" data-testid="pill">
      {PILL[ui.health]}
    </span>
  );
}

export function ModeBar({ ui, a }: { ui: UiState; a: Actions }) {
  const live = ui.mode === "live";
  return (
    <div className="modebar" role="group" aria-label="What the map shows">
      <button aria-pressed={!live} onClick={a.goRecorded}>
        Recorded
      </button>
      <button
        aria-pressed={live}
        disabled={ui.health !== "connected"}
        title={ui.health === "connected" ? undefined : "The live backend is not connected yet."}
        onClick={a.goLive}
      >
        Live
      </button>
    </div>
  );
}

export function PresetPicker({ ui, a }: { ui: UiState; a: Actions }) {
  const current = ui.presets.find((p) => p.preset === ui.preset);
  return (
    <section className="panel presets" aria-labelledby="presets-h">
      <h2 id="presets-h">Recorded presets</h2>
      {ui.presets.length === 0 && !ui.error && <p className="muted">No recorded presets found.</p>}
      <div className="row" role="group" aria-label="Preset traces">
        {ui.presets.map((p) => (
          <button
            key={p.preset}
            aria-pressed={ui.mode === "recorded" && ui.preset === p.preset}
            onClick={() => a.selectPreset(p.preset)}
          >
            {p.name}
          </button>
        ))}
      </div>
      {ui.mode === "recorded" && current && <p className="muted">{current.description}</p>}
      {ui.mode === "recorded" && <Playback ui={ui} a={a} />}
      {ui.loading && <p className="muted" role="status">Loading the trace…</p>}
      {ui.error && <p className="notice" role="alert">{ui.error}</p>}
    </section>
  );
}

function Playback({ ui, a }: { ui: UiState; a: Actions }) {
  const playing = ui.play === "playing";
  return (
    <div className="row playback" role="group" aria-label="Playback">
      <button onClick={playing ? a.pause : a.play} disabled={ui.loading || ui.total === 0}>
        {playing ? "Pause" : ui.play === "ended" ? "Replay" : "Play"}
      </button>
      <button onClick={a.restart} disabled={ui.loading || ui.total === 0}>
        Restart
      </button>
      <div role="group" aria-label="Speed" className="speeds">
        {SPEEDS.map((s) => (
          <button key={s} aria-pressed={ui.speed === s} onClick={() => a.setSpeed(s)}>
            {s}x
          </button>
        ))}
      </div>
      <progress max={ui.total || 1} value={ui.delivered} aria-label="Trace progress" />
      <span className="muted">
        {ui.delivered}/{ui.total} events
      </span>
    </div>
  );
}

export function PromptBox({ ui, a }: { ui: UiState; a: Actions }) {
  const [text, setText] = useState("");
  const off = ui.liveOffReason;
  return (
    <form
      className="panel prompt"
      aria-label="Send a goal"
      title={off || undefined}
      onSubmit={(e) => {
        e.preventDefault();
        if (text.trim()) void a.submitPrompt(text).then(() => setText(""));
      }}
    >
      <label>
        Your own goal
        <input
          value={text}
          maxLength={500}
          disabled={!!off}
          placeholder="e.g. Plan a weekend in Lisbon"
          onChange={(e) => setText(e.target.value)}
        />
      </label>
      <button className="primary" type="submit" disabled={!!off || !text.trim()}>
        Plan it
      </button>
      {off && <p className="muted off">{off}</p>}
    </form>
  );
}
