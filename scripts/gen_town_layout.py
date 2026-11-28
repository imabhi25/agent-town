"""Write docs/reference/town-layout.json: the whole town as data (blocks of building lots, the
square, streets, the path graph, gates, decoration). Deterministic: a fixed seed, so the committed
JSON can always be regenerated and compared. The web app and its tests read the JSON; nothing is
hard-coded in scene code.

    python scripts/gen_town_layout.py

The plan follows the owner's mockup: a square town on a dark board. In the middle, a paved square
with a fountain in front of the town hall (where the Foreman stands). Two long cobblestone streets
run east-west above and below the square, joined by two short side streets, so the square sits
inside a loop of streets. Six blocks of cottage lots face those streets, three below (stages 1-3,
nearest the camera, left to right) and three above (stages 4-6, right to left). Pine trees line the
edge of the board and the gaps between blocks, lamps line the streets.

Coordinates are metres on a 34 x 32 board: x to the right, z towards the viewer, origin at the centre.
"""

import itertools
import json
import math
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "reference" / "town-layout.json"
SEED = 11
rnd = random.Random(SEED)

BOARD = {"w": 34, "d": 32, "corner": 0.6}
STREET_Z = 5.0  # the two east-west streets
SIDE_X = 7.0  # the two short north-south streets that close the loop round the square
STREET_END = 15.5
STREET_W, LANE_W, FOOT_W = 1.7, 0.8, 0.7
BLOCK_X = [-10.5, 0.0, 10.5]
BLOCK_W, BLOCK_D = 9.2, 7.2  # paved block, lots inside
LOT = 1.9  # lot size (a plan has at most 8 tasks, so 8 lots per block)
LOT_X = [
    -1.25,
    1.25,
    -3.65,
    3.65,
]  # front row first, from the middle out: the first tasks are central
FRONT, BACK = 2.3, 6.3  # lot row distance from the street centreline
LANE = 4.3  # the lane between the rows, where the block's hub is
FOUNTAIN = [0.0, 1.8]
HALL = {"x": 0.0, "z": -2.8, "w": 5.0, "d": 2.2}

nodes: dict[str, list[float]] = {}
roads: list[dict] = []


def r2(v):
    return [round(v[0], 2), round(v[1], 2)]


def node(name, x, z):
    nodes[name] = r2([x, z])
    return name


def add_road(id, type, width, a, b):
    roads.append(
        dict(
            id=id,
            type=type,
            width=width,
            **{"from": a, "to": b},
            interior=False,
            points=[nodes[a], nodes[b]],
        )
    )


# ---- the square: fountain, idle pads round it, the Foreman in front of the town hall -------------
node("foreman", 0.0, -1.0)
pads = []
for k in range(8):
    a = math.radians(22.5 + k * 45)
    pads.append(r2([FOUNTAIN[0] + 2.1 * math.cos(a), FOUNTAIN[1] + 2.1 * math.sin(a)]))
    nodes[f"pad{k + 1}"] = pads[-1]

# ---- streets -------------------------------------------------------------------------------------
# South street (z = +5) and north street (z = -5), each split at the block entrances and the side
# street junctions so walks can turn there.
for side, sz in (("s", STREET_Z), ("n", -STREET_Z)):
    stops = [("end_w", -STREET_END)]
    for i, bx in enumerate(BLOCK_X):
        stops.append((f"g{i}", bx))
    stops += [("jw", -SIDE_X), ("je", SIDE_X), ("end_e", STREET_END)]
    stops.sort(key=lambda s: s[1])
    names = [node(f"{side}_{n}", x, sz) for n, x in stops]
    for a, b in itertools.pairwise(names):
        add_road(f"street_{a}_{b}", "street", STREET_W, a, b)
# side streets, split at the middle where the square's paths join them
for side, sx in (("w", -SIDE_X), ("e", SIDE_X)):
    node(f"side_{side}", sx, FOUNTAIN[1])
    add_road(f"street_n_j{side}_side_{side}", "street", STREET_W, f"n_j{side}", f"side_{side}")
    add_road(f"street_side_{side}_s_j{side}", "street", STREET_W, f"side_{side}", f"s_j{side}")

# the square's paths: pads reach the nearest of the west, east and south exits; the Foreman's pad
for k, p in enumerate(pads, start=1):
    dx, dz = p[0] - FOUNTAIN[0], p[1] - FOUNTAIN[1]
    exit_ = "s_g1" if dz > 1.0 else ("side_w" if dx < 0 else "side_e")
    add_road(f"plaza_pad{k}", "plaza", 1.0, f"pad{k}", exit_)
add_road("foot_foreman", "foot", FOOT_W, "foreman", "pad6")

# ---- blocks (zones) ------------------------------------------------------------------------------
# Stage order: 1-3 along the south street from west to east, 4-6 along the north street from east
# to west, so the stages go round the square.
BLOCKS = [("s", 0, +1), ("s", 1, +1), ("s", 2, +1), ("n", 2, -1), ("n", 1, -1), ("n", 0, -1)]
zones = []
gates = []
for n, (street, bi, sgn) in enumerate(BLOCKS, start=1):
    zid = f"z{n}"
    bx = BLOCK_X[bi]
    sz = sgn * STREET_Z
    cz = sz + sgn * (BLOCK_D / 2 + STREET_W / 2 + 0.25)
    gate = node(f"{zid}_gate", bx, sz + sgn * 1.25)
    hub = node(f"{zid}_hub", bx, sz + sgn * LANE)
    add_road(f"lane{n}", "lane", LANE_W, f"{street}_g{bi}", gate)
    add_road(f"in{n}", "foot", FOOT_W, gate, hub)
    slots = []
    for row, dz in enumerate((FRONT, BACK)):
        for k, dx in enumerate(LOT_X):
            i = row * 4 + k + 1
            x, z = bx + dx + rnd.uniform(-0.05, 0.05), sz + sgn * dz + rnd.uniform(-0.05, 0.05)
            slots.append(
                {
                    "id": f"{zid}_s{i}",
                    "x": round(x, 2),
                    "z": round(z, 2),
                    "size": LOT,
                    "rot": round(rnd.uniform(-3, 3), 1),  # doors face the viewer
                }
            )
            nodes[f"{zid}_s{i}"] = [round(x, 2), round(z, 2)]
            add_road(f"{zid}_p{i}", "foot", FOOT_W, hub, f"{zid}_s{i}")
    zones.append(
        {
            "id": zid,
            "stage": n,
            "x": bx,
            "z": round(cz, 2),
            "r": round(math.hypot(BLOCK_W, BLOCK_D) / 2, 2),
            "w": BLOCK_W,
            "d": BLOCK_D,
            "gate": gate,
            "hub": hub,
            "slots": slots,
        }
    )
    # the circuit breaker's barrier, across the block entrance
    gates.append(
        {
            "id": f"barrier{n}",
            "road": f"lane{n}",
            "x": bx,
            "z": round(sz + sgn * 1.0, 2),
            "rot": 90.0 if sgn > 0 else -90.0,
        }
    )

# ---- decoration ----------------------------------------------------------------------------------
street_segs = [(r["points"], r["width"]) for r in roads if r["type"] in ("street", "lane")]


def seg_dist(p, a, b):
    ax, az = a
    bx_, bz = b
    vx, vz = bx_ - ax, bz - az
    L2 = vx * vx + vz * vz or 1e-9
    t = max(0.0, min(1.0, ((p[0] - ax) * vx + (p[1] - az) * vz) / L2))
    return math.hypot(p[0] - (ax + vx * t), p[1] - (az + vz * t))


def road_clear(x, z):
    return min(seg_dist((x, z), a, b) - w / 2 for (a, b), w in street_segs)


def block_clear(x, z):
    """Distance outside the nearest block rectangle (negative = inside)."""
    best = 1e9
    for zz in zones:
        dx = abs(x - zz["x"]) - zz["w"] / 2
        dz = abs(z - zz["z"]) - zz["d"] / 2
        best = min(best, max(dx, dz))
    return best


def square_clear(x, z):
    """Distance outside the square (the area inside the street loop)."""
    return max(abs(x) - (SIDE_X - STREET_W / 2), abs(z) - (STREET_Z - STREET_W / 2))


props = []


def free(x, z, gap):
    return all(math.hypot(x - p["x"], z - p["z"]) >= gap for p in props)


def put(kind, n, where, gap, smin, smax, colors=0, tries=8000):
    c = 0
    while c < n and tries > 0:
        tries -= 1
        x, z = where()
        if road_clear(x, z) < 0.6 or block_clear(x, z) < 0.3 or square_clear(x, z) < 0.3:
            continue
        if abs(x) > BOARD["w"] / 2 - 0.7 or abs(z) > BOARD["d"] / 2 - 0.7 or not free(x, z, gap):
            continue
        d = {
            "kind": kind,
            "x": round(x, 2),
            "z": round(z, 2),
            "s": round(rnd.uniform(smin, smax), 2),
        }
        if colors:
            d["c"] = rnd.randrange(colors)
        props.append(d)
        c += 1


def border():
    """The band round the edge of the board, beyond the streets and blocks."""
    hw, hd = BOARD["w"] / 2, BOARD["d"] / 2
    side = rnd.random()
    if side < 0.5:
        return rnd.uniform(-hw, hw), rnd.choice([-1, 1]) * rnd.uniform(hd - 2.6, hd)
    return rnd.choice([-1, 1]) * rnd.uniform(hw - 1.4, hw), rnd.uniform(-hd, hd)


def anywhere():
    return rnd.uniform(-BOARD["w"] / 2, BOARD["w"] / 2), rnd.uniform(
        -BOARD["d"] / 2, BOARD["d"] / 2
    )


# lamps first, on both sides of the streets, every few metres, clear of the block entrances
for sz in (STREET_Z, -STREET_Z):
    for x in [-14.0, -8.6, -3.4, 3.4, 8.6, 14.0]:
        for off in (-1, 1):
            lx, lz = x + (0.9 if off < 0 else -0.9), sz + off * (STREET_W / 2 + 0.35)
            if (
                all(abs(lx - bx) > 1.4 for bx in BLOCK_X)
                and block_clear(lx, lz) > 0.2
                and road_clear(lx, lz) > 0.15
            ):
                props.append({"kind": "lamp", "x": round(lx, 2), "z": round(lz, 2), "s": 1.0})
for sx in (-SIDE_X, SIDE_X):
    for z in (-2.6, 2.6):
        lx = sx + (STREET_W / 2 + 0.35) * (1 if sx > 0 else -1)
        props.append({"kind": "lamp", "x": round(lx, 2), "z": z, "s": 1.0})

put("tree", 46, border, 1.25, 0.85, 1.3, 3)  # pines (c 0-1) and a few round trees (c 2)
put("tree", 14, anywhere, 1.2, 0.8, 1.15, 3)
put("bush", 26, anywhere, 0.9, 0.6, 1.0, 2)
put("flower", 34, anywhere, 0.6, 0.8, 1.0, 4)
put("rock", 8, border, 1.0, 0.5, 0.9)

doc = {
    "version": 3,
    "seed": SEED,
    "board": BOARD,
    "plaza": {
        "x": 0,
        "z": 0,
        "r": 3.2,
        "w": round(2 * SIDE_X - STREET_W, 2),
        "d": round(2 * STREET_Z - STREET_W, 2),
        "fountain": FOUNTAIN,
        "hall": HALL,
        "foreman": nodes["foreman"],
        "podium_r": 0.7,
        "pads": pads,
    },
    "zones": zones,
    "nodes": nodes,
    "roads": roads,
    "gates": gates,
    "props": props,
    # What the static scene shows before any event: the stages of the happy-path preset.
    "preview": {
        "zones": [
            {"stage": 1, "name": "Research", "tasks": 1},
            {"stage": 2, "name": "Outline + Draft", "tasks": 2},
            {"stage": 3, "name": "Review and publish", "tasks": 1},
        ]
    },
}
OUT.write_text(json.dumps(doc, indent=1) + "\n")
print(
    f"wrote {OUT} ({OUT.stat().st_size} bytes): {len(nodes)} nodes, {len(roads)} roads, {len(props)} props"
)
