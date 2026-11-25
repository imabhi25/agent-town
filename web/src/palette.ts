// Dusk palette for the accessible 2D fallback. Pixi reads these numbers directly.
export const PALETTE = {
  bg: 0x14111c,
  ground: 0x3f364e,
  ink: 0x272131,
  label: 0xeee7f4,
  planner: 0x58446c, // lavender
  queue: 0x604d39, // apricot
  yard: 0x3e534b, // sage
  downstream: 0x394e63, // sky
  done: 0x385645, // mint
  dlq: 0x573e49, // rose
  // worker states
  idle: 0xb7b0c4, // dusty grey-violet
  busy: 0x7fb5ef, // blue
  retrying: 0xf6a85b, // orange
  failed: 0xe8707a, // red
  success: 0x6cc788, // green
  gateOpen: 0x6cc788,
  gateClosed: 0xe8707a,
} as const;

export type WorkerStateName = "idle" | "busy" | "retrying" | "failed" | "success";
