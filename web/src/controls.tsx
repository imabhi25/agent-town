// Map status and sidebar controls.
import { useState } from "react";
import { Icon } from "./Icon";
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
    <span className={`pill ${ui.health}`} role="status" title={PILL[ui.health]} data-testid="pill">
      {ui.health === "waking" ? "Connecting" : ui.health === "offline" ? "Backend offline" : "Backend connected"}
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
    <section className="presets" aria-labelledby="presets-h">
      <h3 id="presets-h">Recorded presets</h3>
      {ui.presets.length === 0 && !ui.error && <p className="muted">No recorded presets found.</p>}
      <div className="row" role="group" aria-label="Preset traces">
        {ui.presets.map((p) => (
          <button
            key={p.preset}
            aria-pressed={ui.mode === "recorded" && ui.preset === p.preset}
            title={p.description}
            onClick={() => a.selectPreset(p.preset)}
          >
            <Icon name="map" />{p.name}<Icon name="chevron" />
          </button>
        ))}
      </div>
      {ui.mode === "recorded" && current && <p className="sr-only">{current.description}</p>}
      {ui.loading && <p className="muted" role="status">Loading the trace…</p>}
      {ui.error && <p className="notice" role="alert">{ui.error}</p>}
    </section>
  );
}

export function Playback({ ui, a }: { ui: UiState; a: Actions }) {
  const playing = ui.play === "playing";
  const disabled = ui.loading || ui.total === 0;
  return (
    <section className="panel playback" role="group" aria-labelledby="playback-h">
      <h2 id="playback-h">Playback</h2>
      <div className="playback-buttons">
        <button className="play-toggle" onClick={playing ? a.pause : a.play} disabled={disabled}>
          <Icon name={playing ? "pause" : "play"} />
          {playing ? "Pause" : ui.play === "ended" ? "Replay" : "Play"}
        </button>
        <button onClick={a.restart} disabled={disabled}>
          <Icon name="restart" /> Restart
        </button>
        <select aria-label="Playback speed" value={ui.speed} disabled={disabled} onChange={(e) => a.setSpeed(Number(e.target.value))}>
          {SPEEDS.map((speed) => <option key={speed} value={speed}>{speed}×</option>)}
        </select>
      </div>
      <div className="playback-progress">
        <progress max={ui.total || 1} value={ui.delivered} aria-label="Trace progress" />
        <span className="muted">{ui.delivered}/{ui.total} events</span>
      </div>
    </section>
  );
}

export function PromptBox({ ui, a, unavailable = "" }: { ui: UiState; a: Actions; unavailable?: string }) {
  const [text, setText] = useState("");
  const off = ui.liveOffReason || unavailable;
  return (
    <form
      className="prompt"
      aria-label="Send a goal"
      title={off || undefined}
      onSubmit={(e) => {
        e.preventDefault();
        if (text.trim()) void a.submitPrompt(text).then(() => setText(""));
      }}
    >
      <label>
        <span className="sr-only">Your own goal</span>
        <textarea
          rows={2}
          aria-describedby={off ? "goal-availability" : undefined}
          value={text}
          maxLength={500}
          disabled={!!off}
          placeholder="Plan a weekend in Lisbon…"
          onChange={(e) => setText(e.target.value)}
        />
      </label>
      <button className="primary" type="submit" disabled={!!off || !text.trim()}>
        <Icon name="play" /> Run goal
      </button>
      {off && <p className="sr-only" id="goal-availability">{off}</p>}
    </form>
  );
}
