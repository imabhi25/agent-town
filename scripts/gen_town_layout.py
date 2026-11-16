"""Write docs/reference/town-layout.json: the whole town as data (zones, building slots, roads, the
path graph, gates, decoration). Deterministic: a fixed seed, so the committed JSON can always be
regenerated and compared. The web app and its tests read the JSON; nothing is hard-coded in scene code.

    python scripts/gen_town_layout.py

Coordinates are metres on a 40 x 30 board: x to the right, z towards the viewer, origin at the centre.
"""

import json
import math
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "reference" / "town-layout.json"
SEED = 11
rnd = random.Random(SEED)

BOARD = {"w": 40, "d": 30, "corner": 1.2}
ZONE_R = 4.6
SLOT_COUNT = 8  # a plan has at most 8 tasks, so a zone never needs more
SLOT_RING = 3.0
SLOT_SIZE = 1.4
PLAZA_R = 3.4
RING_W, SPOKE_W, FOOT_W = 1.8, 1.7, 0.7

# Six zone sites, in plan-stage order (stage 1 .. stage 6) around the centre plaza. The ring road
# visits them in this order, so the main route reads as one loop.
ZONES = [
    ("z1", -13.0, 1.5),
    ("z2", -9.0, -8.0),
    ("z3", 3.0, -9.5),
    ("z4", 13.0, -5.0),
    ("z5", 12.0, 8.0),
    ("z6", -3.0, 9.5),
]

nodes: dict[str, list[float]] = {}
roads: list[dict] = []


def r2(v):
    return [round(v[0], 2), round(v[1], 2)]


def add_road(id, type, width, a, b, mid=(), interior=False):
    pts = [nodes[a]] + [r2(p) for p in mid] + [nodes[b]]
    roads.append(
        dict(id=id, type=type, width=width, **{"from": a, "to": b}, interior=interior, points=pts)
    )


def toward(frm, to, d):
    dx, dz = to[0] - frm[0], to[1] - frm[1]
    L = math.hypot(dx, dz)
    return [frm[0] + dx / L * d, frm[1] + dz / L * d], math.atan2(dz, dx)


# ---- centre plaza: foreman's podium, idle pads, gate nodes on a ring road -----------------------
nodes["foreman"] = [0.0, 0.0]
pads = []
for k in range(8):
    a = math.radians(22.5 + k * 45)
    p = r2([1.7 * math.cos(a), 1.7 * math.sin(a)])
    pads.append(p)
    nodes[f"pad{k + 1}"] = p

zones = []
for zid, cx, cz in ZONES:
    n = zid[1:]
    e, theta = toward(
        (cx, cz), (0, 0), ZONE_R + 0.8
    )  # gate: just outside the perimeter, facing the plaza
    nodes[f"{zid}_gate"] = r2(e)
    nodes[f"{zid}_hub"] = [cx, cz]
    g, _ = toward((0, 0), (cx, cz), 2.9)
    nodes[f"g{n}"] = r2(g)
    slots = []
    for k in range(SLOT_COUNT):
        a = theta + math.radians(22.5 + k * 45 + rnd.uniform(-3, 3))
        rad = SLOT_RING + rnd.uniform(-0.08, 0.08)
        x, z = cx + rad * math.cos(a), cz + rad * math.sin(a)
        slots.append(
            {
                "id": f"{zid}_s{k + 1}",
                "x": round(x, 2),
                "z": round(z, 2),
                "size": SLOT_SIZE,
                "rot": round(rnd.uniform(-8, 8), 1),
            }
        )
        nodes[f"{zid}_s{k + 1}"] = [round(x, 2), round(z, 2)]
    zones.append(
        {
            "id": zid,
            "stage": int(n),
            "x": cx,
            "z": cz,
            "r": ZONE_R,
            "gate": f"{zid}_gate",
            "hub": f"{zid}_hub",
            "slots": slots,
        }
    )

# plaza ring: gates in angular order joined by arcs
order = sorted(range(1, 7), key=lambda i: math.atan2(nodes[f"g{i}"][1], nodes[f"g{i}"][0]))
for i, a in enumerate(order):
    b = order[(i + 1) % 6]
    a0 = math.atan2(nodes[f"g{a}"][1], nodes[f"g{a}"][0])
    a1 = math.atan2(nodes[f"g{b}"][1], nodes[f"g{b}"][0])
    while a1 <= a0:
        a1 += 2 * math.pi
    steps = max(1, int((a1 - a0) / 0.5))
    mid = [
        [
            2.9 * math.cos(a0 + (a1 - a0) * t / (steps + 1)),
            2.9 * math.sin(a0 + (a1 - a0) * t / (steps + 1)),
        ]
        for t in range(1, steps + 1)
    ]
    add_road(f"plaza_{a}_{b}", "plaza", 1.4, f"g{a}", f"g{b}", mid)
# pads and the foreman reach the nearest gate by footpath
for k in range(8):
    p = nodes[f"pad{k + 1}"]
    near = min(range(1, 7), key=lambda i: math.dist(p, nodes[f"g{i}"]))
    add_road(f"foot_pad{k + 1}", "foot", FOOT_W, f"pad{k + 1}", f"g{near}")
add_road("foot_foreman", "foot", FOOT_W, "foreman", "pad2")

# spokes: plaza gate -> zone gate, with a gentle bend so they wind
BEND = [1.0, -1.0, 0.8, -1.0, 1.0, -0.8]
for i, z in enumerate(zones, start=1):
    a, b = nodes[f"g{i}"], nodes[f"z{i}_gate"]
    mx, mz = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    L = math.dist(a, b)
    nx, nz = -(b[1] - a[1]) / L, (b[0] - a[0]) / L
    add_road(
        f"spoke{i}",
        "spoke",
        SPOKE_W,
        f"g{i}",
        f"z{i}_gate",
        [[mx + nx * BEND[i - 1], mz + nz * BEND[i - 1]]],
    )
    add_road(f"in{i}", "foot", FOOT_W + 0.2, f"z{i}_gate", f"z{i}_hub")
    for k in range(SLOT_COUNT):
        add_road(f"z{i}_p{k + 1}", "foot", FOOT_W, f"z{i}_hub", f"z{i}_s{k + 1}")

# the ring: stage 1 -> 2 -> ... -> 6 -> back to 1, passing every zone gate, bowing outwards
for i in range(1, 7):
    j = i % 6 + 1
    a, b = nodes[f"z{i}_gate"], nodes[f"z{j}_gate"]
    mx, mz = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    L = math.dist(a, b)
    out = math.hypot(mx, mz) or 1
    k = 1.6 + (i % 2) * 0.8
    c1 = [
        a[0] + (b[0] - a[0]) * 0.33 + mx / out * k * 0.6,
        a[1] + (b[1] - a[1]) * 0.33 + mz / out * k * 0.9,
    ]
    c2 = [
        a[0] + (b[0] - a[0]) * 0.66 + mx / out * k,
        a[1] + (b[1] - a[1]) * 0.66 + mz / out * k * 0.5,
    ]
    # keep the bends clear of both zones' slots (a ring of 3.0 m plus half a slot and half the road)
    for c, z in ((c1, zones[i - 1]), (c2, zones[j - 1])):
        d = math.hypot(c[0] - z["x"], c[1] - z["z"])
        if d < ZONE_R + 1.7:
            c[0] = z["x"] + (c[0] - z["x"]) / d * (ZONE_R + 1.7)
            c[1] = z["z"] + (c[1] - z["z"]) / d * (ZONE_R + 1.7)
    add_road(f"ring{i}", "ring", RING_W, f"z{i}_gate", f"z{j}_gate", [c1, c2])


def catmull(pts, n=20):
    out = []
    P = [pts[0]] + pts + [pts[-1]]
    for i in range(1, len(P) - 2):
        p0, p1, p2, p3 = P[i - 1], P[i], P[i + 1], P[i + 2]
        for s in range(n):
            t = s / n
            t2, t3 = t * t, t * t * t
            out.append(
                [
                    0.5
                    * (
                        2 * p1[j]
                        + (-p0[j] + p2[j]) * t
                        + (2 * p0[j] - 5 * p1[j] + 4 * p2[j] - p3[j]) * t2
                        + (-p0[j] + 3 * p1[j] - 3 * p2[j] + p3[j]) * t3
                    )
                    for j in range(2)
                ]
            )
    out.append(pts[-1])
    return out


# a barrier on each spoke, a third of the way out along the curve (the circuit breaker lowers them all)
gates = []
for i in range(1, 7):
    pts = catmull(next(r for r in roads if r["id"] == f"spoke{i}")["points"])
    k = int(0.35 * (len(pts) - 1))
    (x, z), (x2, z2) = pts[k], pts[k + 1]
    gates.append(
        {
            "id": f"barrier{i}",
            "road": f"spoke{i}",
            "x": round(x, 2),
            "z": round(z, 2),
            "rot": round(math.degrees(math.atan2(z2 - z, x2 - x)), 1),
        }
    )

# ---- decoration ----------------------------------------------------------------------------------


drawn = [
    (catmull(r["points"]), r["width"]) for r in roads if r["type"] in ("ring", "spoke", "plaza")
]
feet = [catmull(r["points"], 6) for r in roads if r["type"] == "foot"]


def road_clear(x, z):
    best = 1e9
    for pts, w in drawn:
        for p in pts:
            best = min(best, math.hypot(p[0] - x, p[1] - z) - w / 2)
    for pts in feet:
        for p in pts:
            best = min(best, math.hypot(p[0] - x, p[1] - z) - 0.5)
    return best


def zone_clear(x, z):
    return min(math.hypot(x - zz["x"], z - zz["z"]) - zz["r"] for zz in zones)


def plaza_clear(x, z):
    return math.hypot(x, z) - (PLAZA_R + 0.3)


def gate_clear(x, z):
    return min(math.hypot(x - g["x"], z - g["z"]) for g in gates)


props = []


def put(kind, n, clear, smin, smax, colors=0, tries=6000):
    c = 0
    while c < n and tries > 0:
        tries -= 1
        x, z = rnd.uniform(-19, 19), rnd.uniform(-14, 14)
        if (
            road_clear(x, z) < clear
            or zone_clear(x, z) < clear
            or plaza_clear(x, z) < clear
            or gate_clear(x, z) < 2
        ):
            continue
        if any(
            math.hypot(x - p["x"], z - p["z"]) < (clear * 0.9 + 0.5 if p["kind"] == kind else 0.9)
            for p in props
        ):
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


put("tree", 24, 1.6, 0.9, 1.4, 3)
put("bush", 22, 1.0, 0.7, 1.1, 2)
put("rock", 8, 1.0, 0.6, 1.0)
put("flower", 30, 0.8, 0.8, 1.0, 4)
# lamps just outside the spokes
for i in range(1, 7):
    pts = catmull(next(r for r in roads if r["id"] == f"spoke{i}")["points"])
    for t in (0.2, 0.62):
        p = pts[int(t * (len(pts) - 1))]
        q = pts[min(len(pts) - 1, int(t * (len(pts) - 1)) + 1)]
        L = math.hypot(q[0] - p[0], q[1] - p[1]) or 1
        nx, nz = -(q[1] - p[1]) / L, (q[0] - p[0]) / L
        for sgn in (1, -1):
            x, z = p[0] + nx * (SPOKE_W / 2 + 0.7) * sgn, p[1] + nz * (SPOKE_W / 2 + 0.7) * sgn
            if road_clear(x, z) > 0.4 and gate_clear(x, z) > 1.6 and zone_clear(x, z) > 0.4:
                props.append({"kind": "lamp", "x": round(x, 2), "z": round(z, 2), "s": 1.0})
                break
# fences: segments along each zone's perimeter, leaving a gap at the entrance
for zz in zones:
    gx, gz = nodes[zz["gate"]]
    ent = math.atan2(gz - zz["z"], gx - zz["x"])
    n = 26
    for k in range(n):
        a = ent + math.radians(30) + (2 * math.pi - math.radians(60)) * (k + 0.5) / n
        x, z = zz["x"] + (zz["r"] - 0.15) * math.cos(a), zz["z"] + (zz["r"] - 0.15) * math.sin(a)
        if abs(x) < 19 and abs(z) < 14.2 and road_clear(x, z) > 0.5:
            props.append(
                {
                    "kind": "fence",
                    "x": round(x, 2),
                    "z": round(z, 2),
                    "s": 1.0,
                    "rot": round(-math.degrees(a) + 90, 1),
                }
            )

doc = {
    "version": 2,
    "seed": SEED,
    "board": BOARD,
    "plaza": {"x": 0, "z": 0, "r": PLAZA_R, "foreman": [0.0, 0.0], "podium_r": 0.9, "pads": pads},
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
