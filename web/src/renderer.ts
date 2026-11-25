// PixiJS view of a Scene. It owns no game logic: each frame it (1) hands the scene any new store
// events, (2) steps the animation, (3) copies numbers onto pre-built sprites.
//
// Frame budget rules: every sprite and Text is created once up front (the worker pool is
// MAX_WORKERS sprites, reused); the ticker callback allocates nothing; Text is only touched when
// its string changed; events are applied in ONE batch per frame, and only when the store changed.
import { Application, Container, Graphics, Sprite, Text, Texture } from "pixi.js";
import { PALETTE, type WorkerStateName } from "./palette";
import { BUILDINGS, CAPTIONS, GATE, MAX_WORKERS, Scene, WORLD_H, WORLD_W, type Building } from "./scene";
import type { StoreState } from "./store";

const FONT = "ui-rounded, 'SF Pro Rounded', system-ui, sans-serif";
const QUEUE_TOKENS = 12;
const DLQ_TOKENS = 8;

export interface RendererHooks {
  getState: () => StoreState;
  onChange: () => void; // scene changed in a way the DOM overlays care about
  onWorkerTap: (workerId: string) => void;
}

const lerpColor = (a: number, b: number, t: number): number => {
  const ar = (a >> 16) & 255, ag = (a >> 8) & 255, ab = a & 255;
  const br = (b >> 16) & 255, bg = (b >> 8) & 255, bb = b & 255;
  return ((ar + (br - ar) * t) << 16) | ((ag + (bg - ag) * t) << 8) | (ab + (bb - ab) * t);
};

export class CityRenderer {
  readonly scene = new Scene();
  fps = 0;
  private app = new Application();
  private world = new Container();
  private workers: Container[] = [];
  private bodies2: Sprite[] = []; // worker body sprites (tinted by state)
  private labels: Text[] = [];
  private bodies: Container[] = [];
  private queueTokens: Sprite[] = [];
  private dlqTokens: Sprite[] = [];
  private texts = new Map<string, Text>();
  private gateArm!: Sprite;
  private lastEvents: unknown = null;
  private lastSnapshot: unknown = null;
  private lastVersion = -1;
  private destroyed = false;

  constructor(private host: HTMLElement, private hooks: RendererHooks) {}

  async init(): Promise<void> {
    await this.app.init({
      background: PALETTE.bg,
      resizeTo: this.host,
      antialias: true,
      autoDensity: true,
      resolution: Math.min(2, window.devicePixelRatio || 1),
    });
    if (this.destroyed) {
      this.app.destroy(true);
      return;
    }
    this.host.appendChild(this.app.canvas);
    this.app.canvas.setAttribute("aria-hidden", "true"); // the DOM summary is the accessible view
    this.app.stage.addChild(this.world);
    this.build();
    this.layout();
    this.app.renderer.on("resize", () => this.layout());
    this.app.ticker.add((t) => this.frame(t.deltaMS));
  }

  destroy(): void {
    this.destroyed = true;
    try {
      this.app.destroy(true, { children: true });
    } catch {
      /* init may not have finished */
    }
  }

  // ---- one-time construction ---------------------------------------------------------------

  private build(): void {
    // Roads join the buildings: planner|queue, queue-yard, yard-downstream (the gate stands on
    // it), downstream|done, yard|dlq.
    const road = new Graphics()
      .rect(115, 165, 30, 90).fill(PALETTE.ground)
      .rect(205, 285, 100, 30).fill(PALETTE.ground)
      .rect(595, 215, 125, 30).fill(PALETTE.ground)
      .rect(795, 245, 30, 140).fill(PALETTE.ground)
      .rect(435, 390, 30, 60).fill(PALETTE.ground);
    this.world.addChild(road);

    for (const b of Object.values(BUILDINGS)) this.building(b);

    // Tokens: tiny squares in the Queue and DLQ buildings, shown = min(count, N).
    const square = this.squareTexture();
    const q = BUILDINGS.queue;
    for (let i = 0; i < QUEUE_TOKENS; i++) {
      const s = this.token(square, 0xe5bb81, q.x - q.w / 2 + 14 + (i % 6) * 22, q.y - 4 + Math.floor(i / 6) * 22);
      this.queueTokens.push(s);
    }
    const d = BUILDINGS.dlq;
    for (let i = 0; i < DLQ_TOKENS; i++) {
      this.dlqTokens.push(this.token(square, PALETTE.failed, d.x - d.w / 2 + 14 + i * 16, d.y - 4));
    }

    // Circuit gate: an arm hinged at its foot. Vertical = closed (blocks the road), flat = open.
    this.gateArm = new Sprite(square);
    this.gateArm.width = GATE.w;
    this.gateArm.height = GATE.h;
    this.gateArm.anchor.set(0.5, 1);
    this.gateArm.position.set(GATE.x, GATE.y + GATE.h / 2);
    const post = new Graphics().circle(GATE.x, GATE.y + GATE.h / 2, 9).fill(PALETTE.ink);
    this.world.addChild(this.gateArm, post);
    this.text("gate", "circuit: open", GATE.x - 4, GATE.y - GATE.h / 2 - 18, 12, 0.5);

    // Worker pool: a container per worker (body sprite + number), all created now and reused.
    const bodyTex = this.app.renderer.generateTexture(
      new Graphics().circle(14, 14, 13).fill(0xffffff).stroke({ width: 2, color: PALETTE.ink }),
    );
    for (let i = 0; i < MAX_WORKERS; i++) {
      const c = new Container();
      const body = new Sprite(bodyTex);
      body.anchor.set(0.5);
      const label = new Text({ text: "", style: { fontFamily: FONT, fontSize: 11, fill: PALETTE.ink, fontWeight: "700" } });
      label.anchor.set(0.5);
      c.addChild(body, label);
      c.visible = false;
      c.eventMode = "static";
      c.cursor = "pointer";
      c.on("pointertap", () => {
        const w = this.scene.pool[i];
        if (w.active && w.dying === 0) this.hooks.onWorkerTap(w.id);
      });
      this.workers.push(c);
      this.bodies2.push(body);
      this.labels.push(label);
      this.world.addChild(c);
    }
  }

  private building(b: Building): void {
    const g = new Graphics()
      .roundRect(-b.w / 2, -b.h / 2, b.w, b.h, 18)
      .fill(PALETTE[b.id])
      .stroke({ width: 3, color: PALETTE.label, alpha: 0.15 });
    const c = new Container();
    c.addChild(g);
    c.position.set(b.x, b.y);
    this.bodies.push(c);
    this.world.addChild(c);
    this.text(`label:${b.id}`, b.label, b.x, b.y - b.h / 2 + 14, 15, 0.5, true);
    this.text(`caption:${b.id}`, CAPTIONS[b.id], b.x, b.y - b.h / 2 + 32, 11, 0.5);
    if (b.id === "queue" || b.id === "done" || b.id === "dlq" || b.id === "planner") {
      this.text(`count:${b.id}`, "", b.x, b.y + b.h / 2 - 16, 13, 0.5);
    }
  }

  private text(key: string, value: string, x: number, y: number, size: number, anchorX = 0, bold = false): Text {
    const t = new Text({
      text: value,
      style: { fontFamily: FONT, fontSize: size, fill: PALETTE.label, fontWeight: bold ? "700" : "500" },
    });
    t.anchor.set(anchorX, 0.5);
    t.position.set(x, y);
    this.texts.set(key, t);
    this.world.addChild(t);
    return t;
  }

  private squareTexture(): Texture {
    return this.app.renderer.generateTexture(new Graphics().roundRect(0, 0, 16, 16, 4).fill(0xffffff));
  }

  private token(tex: Texture, tint: number, x: number, y: number): Sprite {
    const s = new Sprite(tex);
    s.tint = tint;
    s.alpha = 0.85;
    s.position.set(x, y);
    s.visible = false;
    this.world.addChild(s);
    return s;
  }

  private layout(): void {
    const { width, height } = this.app.screen;
    const s = Math.min(width / WORLD_W, height / WORLD_H);
    this.world.scale.set(s);
    this.world.position.set((width - WORLD_W * s) / 2, (height - WORLD_H * s) / 2);
  }

  // ---- per frame ---------------------------------------------------------------------------

  private frame(deltaMS: number): void {
    const dt = Math.min(deltaMS / 1000, 0.1); // a stalled tab must not teleport everything
    const state = this.hooks.getState();
    let changed = false;
    if (state.events !== this.lastEvents || state.snapshot !== this.lastSnapshot) {
      this.lastEvents = state.events;
      this.lastSnapshot = state.snapshot;
      this.scene.sync(state.snapshot, state.events, state.epoch); // the whole batch for this frame
      changed = true;
    }
    this.scene.step(dt);
    this.draw();
    this.fps = this.app.ticker.FPS;
    if (changed || this.scene.version !== this.lastVersion) {
      this.lastVersion = this.scene.version;
      this.hooks.onChange();
    }
  }

  private setText(key: string, value: string): void {
    const t = this.texts.get(key);
    if (t && t.text !== value) t.text = value;
  }

  private draw(): void {
    const sc = this.scene;
    for (let i = 0; i < MAX_WORKERS; i++) {
      const w = sc.pool[i];
      const s = this.workers[i];
      if (!w.active) {
        if (s.visible) s.visible = false;
        continue;
      }
      s.visible = true;
      s.position.set(w.x, w.y);
      this.bodies2[i].tint = PALETTE[w.state as WorkerStateName];
      const lab = this.labels[i];
      const short = w.id.slice(w.id.lastIndexOf("-") + 1);
      if (lab.text !== short) lab.text = short;
      s.alpha = w.dying > 0 ? w.dying : 1;
      s.scale.set(w.dying > 0 ? 0.6 + 0.4 * w.dying : 1);
    }
    // Pulses: a building swells briefly when an event touches it.
    this.bodies[0].scale.set(1 + sc.plannerPulse * 0.06); // planner
    this.bodies[1].scale.set(1 + sc.queuePulse * 0.06); // queue
    for (let i = 0; i < QUEUE_TOKENS; i++) this.queueTokens[i].visible = i < sc.queueDepth;
    for (let i = 0; i < DLQ_TOKENS; i++) this.dlqTokens[i].visible = i < sc.dead;
    this.setText("count:queue", `${sc.queueDepth} waiting`);
    this.setText("count:done", `${sc.done} done`);
    this.setText("count:dlq", `${sc.dead} dead`);
    this.setText("count:planner", "");
    // Gate: vertical when closed, swung flat when open; red when closed, green when open.
    this.gateArm.rotation = (1 - sc.gate) * (Math.PI / 2);
    this.gateArm.tint = lerpColor(PALETTE.gateOpen, PALETTE.gateClosed, sc.gate);
    this.setText("gate", sc.gate > 0.5 ? "circuit TRIPPED: gate shut" : "circuit ok: gate open");
  }
}
