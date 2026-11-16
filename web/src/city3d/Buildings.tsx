// The six buildings. Each sits on a tinted district plate; colours are the 2D map's state colours.
import { CAPTIONS } from "../scene";
import { PALETTE } from "../palette";
import { Box, Label, Plate, Robot, hex, shade } from "./parts";
import { plot, ROBOT_SLOTS } from "./layout";

const Y0 = 0.13; // top of the plate

function Spot({ id, color, children, labelY }: { id: Parameters<typeof plot>[0]; color: number; children: React.ReactNode; labelY: number }) {
  const q = plot(id);
  return (
    <group position={[q.x, 0, q.z]}>
      <Plate w={q.w} d={q.d} color={color} />
      <group position={[0, Y0, 0]}>{children}</group>
      <group position={[0, Y0, 0]}>
        <Label y={labelY} title={q.label} caption={CAPTIONS[id]} />
      </group>
    </group>
  );
}

const WIN = "#fdfbf6";

/** Planner: a tall lavender tower with a rooftop antenna. */
function Planner() {
  const c = hex(PALETTE.planner);
  return (
    <Spot id="planner" color={PALETTE.planner} labelY={3.9}>
      <Box p={[0, 1.4, 0]} s={[2.0, 2.8, 1.6]} c={shade(PALETTE.planner, 0.97)} />
      <Box p={[0, 2.86, 0]} s={[2.2, 0.16, 1.8]} c={shade(PALETTE.planner, 0.8)} />
      <Box p={[0, 3.1, 0]} s={[1.1, 0.4, 1.0]} c={c} />
      <Box p={[0, 3.55, 0]} s={[0.06, 0.6, 0.06]} c="#9a8fb0" />
      {[-0.6, 0.6].flatMap((x) => [0.7, 1.5, 2.3].map((y) => <Box key={`${x}${y}`} p={[x, y, 0.81]} s={[0.4, 0.4, 0.04]} c={WIN} cast={false} />))}
    </Spot>
  );
}

/** Queue: a long low warehouse with stacked crates waiting outside. */
function Queue() {
  return (
    <Spot id="queue" color={PALETTE.queue} labelY={2.6}>
      <Box p={[0, 0.7, -0.2]} s={[3.2, 1.4, 1.5]} c={shade(PALETTE.queue, 0.96)} />
      <Box p={[0, 1.5, -0.2]} s={[3.4, 0.16, 1.7]} c={shade(PALETTE.queue, 0.78)} />
      <Box p={[0, 0.55, 0.57]} s={[1.0, 1.1, 0.05]} c="#e9c690" cast={false} />
      {[[-1.1, 0.8], [-0.55, 0.8], [-0.8, 0.8]].map(([x, z], i) => (
        <Box key={i} p={[x, 0.22 + (i === 2 ? 0.44 : 0), z + (i === 2 ? 0 : 0.1)]} s={[0.42, 0.42, 0.42]} c="#e8b878" />
      ))}
      <Box p={[1.1, 0.22, 0.85]} s={[0.42, 0.42, 0.42]} c="#e8b878" />
    </Spot>
  );
}

/** Workers: an open pavilion (four posts, a flat roof) with a floor pad per robot. */
function Yard() {
  const post = shade(PALETTE.yard, 0.72);
  return (
    <Spot id="yard" color={PALETTE.yard} labelY={3.1}>
      <Box p={[0, 0.03, 0]} s={[6.6, 0.06, 3.9]} c={shade(PALETTE.yard, 1.05)} cast={false} />
      {[[-3.1, -1.7], [3.1, -1.7], [-3.1, 1.7], [3.1, 1.7]].map(([x, z], i) => (
        <Box key={i} p={[x, 1.2, z]} s={[0.22, 2.4, 0.22]} c={post} />
      ))}
      {/* open roof frame (beams, not a slab) so the robots underneath stay visible from above */}
      {[-1.9, 1.9].map((z) => <Box key={z} p={[0, 2.5, z]} s={[7.0, 0.2, 0.5]} c={shade(PALETTE.yard, 0.82)} />)}
      {[-3.1, 0, 3.1].map((x) => <Box key={x} p={[x, 2.5, 0]} s={[0.4, 0.2, 4.3]} c={shade(PALETTE.yard, 0.82)} />)}
      {ROBOT_SLOTS.map(([x, z], i) => (
        <group key={i}>
          <Box p={[x, 0.09, z]} s={[1.0, 0.06, 1.0]} c={shade(PALETTE.yard, 0.86)} cast={false} />
          <Robot p={[x, 0.12, z]} rotY={0.3 * (i % 2 ? -1 : 1)} />
        </group>
      ))}
    </Spot>
  );
}

/** Downstream / LLM: a sky-blue hall with a golden dome, echoing the domed houses in the reference. */
function Downstream() {
  const gold = "#e9b857";
  return (
    <Spot id="downstream" color={PALETTE.downstream} labelY={3.5}>
      <Box p={[0, 0.9, 0]} s={[2.5, 1.8, 1.8]} c={shade(PALETTE.downstream, 1.02)} />
      <Box p={[0, 1.85, 0]} s={[2.7, 0.16, 2.0]} c={shade(PALETTE.downstream, 0.8)} />
      <mesh position={[0, 1.93, 0]} castShadow>
        <sphereGeometry args={[0.8, 14, 8, 0, Math.PI * 2, 0, Math.PI / 2]} />
        <meshStandardMaterial color={gold} roughness={0.5} flatShading />
      </mesh>
      <Box p={[0, 2.95, 0]} s={[0.05, 0.4, 0.05]} c={gold} />
      {[-0.8, 0, 0.8].map((x) => <Box key={x} p={[x, 0.9, 0.91]} s={[0.4, 0.7, 0.04]} c={WIN} cast={false} />)}
    </Spot>
  );
}

/** Done: a mint house with a pitched roof and a flag. */
function Done() {
  return (
    <Spot id="done" color={PALETTE.done} labelY={3.0}>
      <Box p={[0, 0.7, 0]} s={[2.2, 1.4, 1.5]} c={shade(PALETTE.done, 1.0)} />
      <mesh position={[0, 1.8, 0]} rotation={[0, Math.PI / 4, 0]} castShadow>
        <coneGeometry args={[1.75, 0.9, 4]} />
        <meshStandardMaterial color={shade(PALETTE.done, 0.72)} roughness={0.85} flatShading />
      </mesh>
      <Box p={[0.9, 2.25, 0]} s={[0.05, 1.0, 0.05]} c="#8aa090" />
      <Box p={[1.12, 2.55, 0]} s={[0.4, 0.26, 0.03]} c={hex(PALETTE.success)} cast={false} />
      <Box p={[0, 0.5, 0.76]} s={[0.5, 1.0, 0.04]} c={WIN} cast={false} />
    </Spot>
  );
}

/** DLQ: a small rose building with a dark door and a red band: dead jobs wait here for a human. */
function Dlq() {
  return (
    <Spot id="dlq" color={PALETTE.dlq} labelY={2.1}>
      <Box p={[0, 0.65, 0]} s={[2.0, 1.3, 1.4]} c={shade(PALETTE.dlq, 0.98)} />
      <Box p={[0, 1.38, 0]} s={[2.2, 0.16, 1.6]} c={shade(PALETTE.dlq, 0.78)} />
      <Box p={[0, 0.45, 0.71]} s={[0.55, 0.9, 0.04]} c="#6b5560" cast={false} />
      <Box p={[0, 1.05, 0.72]} s={[1.6, 0.14, 0.03]} c={hex(PALETTE.failed)} cast={false} />
    </Spot>
  );
}

export function Buildings() {
  return (
    <>
      <Planner />
      <Queue />
      <Yard />
      <Downstream />
      <Done />
      <Dlq />
    </>
  );
}
