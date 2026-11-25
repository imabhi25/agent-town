// Colours of the 3D town: a night palette with a lavender accent (the mood of the owner's mockup).
// States keep the project palette (palette.ts); zones reuse its pastels, which read well on the
// dark ground. Warm light comes only from lamps and from the windows of finished buildings.
export const ZONE_COLORS = ["#cdb4f6", "#a9d6f5", "#b8e6c1", "#f9d9a8", "#f5b5b5", "#cfe8d5"];
export const C = {
  bg: "#16121f",
  boardRim: "#3a3148",
  boardSide: "#2c2538",
  grass: "#47614f",
  zoneGrass: "#52705a",
  ground: "#3d3549",
  ringRoad: "#6f6886",
  spokeRoad: "#7b7492",
  plaza: "#8b8299",
  curb: "#544c66",
  arrow: "#c9c0e6",
  foot: "#9a8f9e",
  ghost: "#6e667c",
  ghostEdge: "#a49ab8",
  wall: "#eadcc6", // plaster of a cottage
  roof: "#b45a48", // terracotta, blended with the zone colour per building
  timber: "#8b6a4c", // scaffold posts and boards
  window: "#ffc873", // warm lit windows
  windowOff: "#3b3446",
  lamp: "#ffd98f",
  ink: "#4a4458",
  gold: "#f2c14e",
  hardhat: "#f6c33b",
  lavender: "#b9a3ff",
};
