// Which view to show first. Pure functions so the rule is unit tested without a GPU.
export type ViewMode = "2d" | "3d";

export function supportsWebGL2(): boolean {
  try {
    return !!document.createElement("canvas").getContext("webgl2");
  } catch {
    return false;
  }
}

export function prefersReducedMotion(): boolean {
  return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** `?view=2d` or `?view=3d` forces a view (screenshots, tests, support); anything else is ignored. */
export function parseViewOverride(search: string): ViewMode | null {
  const v = new URLSearchParams(search).get("view");
  return v === "2d" || v === "3d" ? v : null;
}

/** Default is 3D only when WebGL2 works and the user has not asked for reduced motion. */
export function defaultView(opts: {
  webgl2: boolean;
  reducedMotion: boolean;
  override?: ViewMode | null;
}): ViewMode {
  if (opts.override === "2d") return "2d";
  // Even a forced 3D needs WebGL2: without it the 2D map is the only thing that can draw.
  if (!opts.webgl2) return "2d";
  if (opts.override === "3d") return "3d";
  return opts.reducedMotion ? "2d" : "3d";
}
