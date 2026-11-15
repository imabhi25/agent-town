// One pastel palette for the whole UI. The CSS custom properties in styles.css mirror these hex
// values; the Pixi renderer reads the numbers directly.
export const PALETTE = {
  bg: 0xf6f1ea, // warm paper
  ground: 0xe9e2d6,
  ink: 0x4a4458,
  planner: 0xcdb4f6, // lavender
  queue: 0xf9d9a8, // apricot
  yard: 0xcfe8d5, // sage
  downstream: 0xa9d6f5, // sky
  done: 0xb8e6c1, // mint
  dlq: 0xf5b5b5, // rose
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
