import { describe, expect, it } from "vitest";
import { defaultView, parseViewOverride } from "./webgl";

describe("default view", () => {
  it("is 3D when WebGL2 works and motion is not reduced", () => {
    expect(defaultView({ webgl2: true, reducedMotion: false })).toBe("3d");
  });
  it("is 2D when the user prefers reduced motion", () => {
    expect(defaultView({ webgl2: true, reducedMotion: true })).toBe("2d");
  });
  it("is 2D without WebGL2, even if 3D is forced", () => {
    expect(defaultView({ webgl2: false, reducedMotion: false })).toBe("2d");
    expect(defaultView({ webgl2: false, reducedMotion: false, override: "3d" })).toBe("2d");
  });
  it("lets ?view= override reduced motion, and 2D always wins", () => {
    expect(defaultView({ webgl2: true, reducedMotion: true, override: "3d" })).toBe("3d");
    expect(defaultView({ webgl2: true, reducedMotion: false, override: "2d" })).toBe("2d");
  });
  it("parses only valid overrides", () => {
    expect(parseViewOverride("?view=3d")).toBe("3d");
    expect(parseViewOverride("?view=2d")).toBe("2d");
    expect(parseViewOverride("?view=4d")).toBeNull();
    expect(parseViewOverride("")).toBeNull();
  });
});
