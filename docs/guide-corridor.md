# Guide Corridor (Preferred Route)

This document describes the guide corridor feature: draw a polyline on a User layer in KiCad, and selected nets are routed to follow it as a chain of waypoints — getting as close to your drawn path as obstacles allow, without ever making a route fail.

Implementation: parsing in `kicad_parser.py` (`parse_guide_paths()`, `_chain_guide_segments()`), waypoint generation and routing in `single_ended_routing.py` (`build_corridor_waypoints()`, `_route_main_connection()`).

## Usage

1. In KiCad, draw the desired path as graphic lines (`gr_line`, or a `gr_poly` outline) on a User layer — `User.1` by default. Consecutive segments sharing endpoints (within 0.01mm) are chained into one path.
2. Route with the corridor enabled:

```bash
python route.py board.kicad_pcb --nets "SPI*" --guide-corridor
```

In the plugin, tick **"Follow User-layer guide path"** (and optionally **"Clear guide layer after routing"** to remove the drawn lines afterwards).

## Command-Line Options

| Option | Default | Description |
|--------|---------|-------------|
| `--guide-corridor` | false | Route selected nets along the User-layer guide path |
| `--guide-corridor-layer` | `User.1` | Layer the guide polyline is drawn on |
| `--guide-corridor-spacing` | 0.0 | Waypoint subdivision spacing in mm (0 = use the drawn vertices only) |

Supported by `route.py` and the plugin. Not currently supported by `route_diff.py` or `route_planes.py`.

## Waypoint Generation

`build_corridor_waypoints()` converts each guide path into an ordered list of grid waypoints:

- With `--guide-corridor-spacing 0` (default), only the drawn polyline's vertices become waypoints.
- With a positive spacing, long segments are subdivided so that no two consecutive waypoints are farther apart than the spacing. Tighter spacing makes the route hug the drawn line more closely (at the cost of more A* legs); the vertices-only default lets the router take the straightest legal path between corners.

## How Routes Follow the Guide

The net is routed **leg by leg**: source → waypoint₁ → waypoint₂ → … → target, each leg an independent A* search, concatenated at shared endpoints.

The guide is strictly best-effort:

- **Blocked waypoints are skipped.** If a waypoint sits on or too close to an obstacle, the route snaps to the nearest free cell within a clearance-aware margin; if no useful nearby cell exists, the waypoint is dropped and routing continues to the next one.
- **A guide never makes a route fail.** If following the waypoints strands the route (e.g. the approach to the target becomes unreachable), waypoints are popped off and the route backs off; in the worst case the net is routed directly, exactly as it would have been without a guide.
- **A guide never adds vias.** Legs are routed on the layer the route has committed to; a waypoint that can't be followed on the current layer is skipped rather than reached via a layer change the direct route wouldn't have needed.

## Multi-Point Nets

For nets with 3+ pads, the route topology (the MST between pads) is unchanged by the guide. Instead, each waypoint is assigned to the MST edge it is geometrically nearest to (`assign_waypoints_to_mst_edges()`), preserving waypoint order within each edge. Each edge is then routed through its own waypoints, so one drawn line can steer different sections of a multi-point net without altering its endpoints or topology.

## Multiple Nets on One Corridor

Several nets can follow the same drawn line. Nets are routed sequentially, and each routed net becomes an obstacle for the next, so later nets pack alongside earlier ones at legal clearance instead of overlapping — the corridor fills up like a bundle. (For long parallel buses, also consider [bus routing](bus-routing.md), which adds neighbor attraction.)

## Tips

- Don't draw the guide over a pad you need to route to or from — the endpoint legs still need clear access.
- The guide steers only the nets selected for routing in that run (`--nets`); use net selection to control which nets follow which corridor, drawing one corridor per run if different nets need different paths.
- Start with the default spacing; only set `--guide-corridor-spacing` (e.g. 1–2mm) if the route cuts corners you want it to follow faithfully.
