#!/usr/bin/env python3
"""Make a routed board's KiCad DRC settings consistent with the clearances and
sizes it was actually routed to, so an interactive DRC in KiCad shows only the
relevant (routing) errors instead of stock-default noise (issue #160).

KiCad stores DRC *design rules* and *violation severities* in the PROJECT file
(``.kicad_pro``), NOT in the board (``.kicad_pcb``). A freshly written board gets
a project with KiCad's stock defaults, which produce noise in two ways:

  1. Constraint floors stricter than the board was routed to -- e.g. the stock
     ``min_clearance`` 0.2 mm, ``min_via_diameter`` 0.45 mm, ``min_track_width``
     0.2 mm or ``min_hole_clearance`` 0.25 mm -- fire on every track/via/drill
     the router placed below them (hundreds of spurious markers). They are not
     real problems at the manufacturing floor the board was routed to;
     ``check_drc.py`` never reports them.
  2. Placement / fabrication categories (courtyard overlaps, solder-mask bridges,
     footprint-library annular/mismatch) fire even though the router neither
     creates nor fixes them.

This script rewrites the sibling ``.kicad_pro`` so KiCad's enforced
**Board Setup -> Constraints / Net Classes** match the per-object minima the
board actually uses:

  * copper **clearance** (``min_clearance`` + Default net-class clearance)
  * **hole-to-hole** clearance (``min_hole_to_hole``)
  * **hole/copper** clearance (``min_hole_clearance``)
  * **copper-to-edge** clearance (``min_copper_edge_clearance``)
  * **min track width / via diameter / via drill / annular ring** -- lowered to
    the smallest such object actually placed on the board
  * non-routing severities (courtyard, solder-mask, footprint/library) -> ignore

**Only loosen, never tighten.** Every constraint is set to ``min(current, target)``
-- it is only *lowered* toward the real fab floor, never raised. So this can
never introduce a NEW violation or silently strengthen a rule; it only stops
KiCad flagging copper the router legitimately placed. A constraint the user
already set looser than the routed floor is left as-is.

Targets come from the routing parameters when you pass them (``--clearance``,
``--hole-to-hole``, ``--edge-clearance``, ``--track-width``, ``--via-size``,
``--via-drill`` -- match what you gave ``route.py``); track/via/drill/annular also
fall back to the smallest object found on the board, and clearance falls back to
the project's Default net-class clearance.

IMPORTANT: close the board in KiCad before running this. KiCad keeps the project
in memory and will overwrite an externally-edited ``.kicad_pro`` on save/close.

Usage:
    python3 fix_kicad_drc_settings.py board.kicad_pcb [options]
"""
import argparse
import json
import os
import sys

# Severity categories treated as non-routing noise by default.
COURTYARD_CATS = ["courtyards_overlap", "malformed_courtyard",
                  "npth_inside_courtyard", "pth_inside_courtyard"]
MASK_CATS = ["solder_mask_bridge"]
# Footprint / library-geometry issues inherited from the source board's
# footprints (annular rings, pad/footprint library mismatches). The router does
# not create or fix these, so they are pure noise when reviewing a routed board
# -- on the stress boards they dominate the report (e.g. 199 annular_width + 149
# lib_footprint markers on orangecrab). Ignored by default; --keep-footprint
# restores them.
FOOTPRINT_CATS = ["annular_width", "lib_footprint_issues", "lib_footprint_mismatch"]
# Thermal-relief spoke shortfalls (a zone connects a pad with fewer spokes than
# the zone's min). It is a real-but-minor fab detail, not a routing short, so
# demote it from error to a WARNING (still visible, not blocking) rather than
# hiding it. --keep-thermal leaves it an error.
WARNING_CATS = ["starved_thermal"]

# Severity rank for "only loosen" comparisons (higher = stricter).
_SEV_RANK = {"error": 2, "warning": 1, "ignore": 0}

# A complete KiCad "Default" net class. KiCad only honours a net class it
# considers well-formed; a sparse {name, clearance, ...} stub is silently
# dropped and the board falls back to the stock 0.2 mm default (issue #160
# v9 demo). Used only when the project has NO Default class (a bare/stub
# project); a real KiCad-written project already has a complete one we just edit.
_DEFAULT_NETCLASS = {
    "bus_width": 12, "clearance": 0.2, "diff_pair_gap": 0.25,
    "diff_pair_via_gap": 0.25, "diff_pair_width": 0.2, "line_style": 0,
    "microvia_diameter": 0.3, "microvia_drill": 0.2, "name": "Default",
    "pcb_color": "rgba(0, 0, 0, 0.000)", "priority": 2147483647,
    "schematic_color": "rgba(0, 0, 0, 0.000)", "track_width": 0.2,
    "via_diameter": 0.6, "via_drill": 0.3, "wire_width": 6,
}


def find_project(path: str) -> str:
    """Return the .kicad_pro path for a .kicad_pcb / .kicad_pro / base path."""
    base, ext = os.path.splitext(path)
    pro = path if ext == ".kicad_pro" else base + ".kicad_pro"
    return pro


def project_copper_clearance(proj: dict):
    """The board's copper clearance: the Default netclass clearance, else
    rules.min_clearance. Returns None if neither is set (>0)."""
    for cls in proj.get("net_settings", {}).get("classes", []):
        if cls.get("name") == "Default" and cls.get("clearance"):
            return cls["clearance"]
    classes = proj.get("net_settings", {}).get("classes", [])
    if classes and classes[0].get("clearance"):
        return classes[0]["clearance"]
    mc = proj.get("board", {}).get("design_settings", {}).get("rules", {}).get("min_clearance")
    return mc if mc else None


def scan_board_minima(pcb_path: str):
    """Smallest track width / via diameter / via drill / via annular ring / hole
    diameter actually present on the board. These are floors KiCad's min-size
    rules must sit at or below, or it flags the board's own copper. Returns a
    dict of floats (missing keys absent). Best-effort -- returns {} if the board
    can't be parsed."""
    if not os.path.isfile(pcb_path):
        return {}
    try:
        from kicad_parser import parse_kicad_pcb
        pcb = parse_kicad_pcb(pcb_path)
    except Exception as e:  # pragma: no cover - parser is robust, but stay safe
        print(f"warning: could not scan board minima ({e})", file=sys.stderr)
        return {}

    out = {}
    widths = [s.width for s in pcb.segments if s.width and s.width > 0]
    if widths:
        out["min_track_width"] = min(widths)
    via_drills = [v.drill for v in pcb.vias if v.drill]
    if pcb.vias:
        sizes = [v.size for v in pcb.vias if v.size]
        if sizes:
            out["min_via_diameter"] = min(sizes)
        if via_drills:
            out["min_via_drill"] = min(via_drills)
        annular = [(v.size - v.drill) / 2.0 for v in pcb.vias
                   if v.size and v.drill and v.size > v.drill]
        if annular:
            out["min_via_annular_width"] = min(annular)
    # Through-hole pad / via drills set the smallest hole diameter on the board.
    hole = list(via_drills)
    for fp in pcb.footprints.values():
        for pad in fp.pads:
            if getattr(pad, "drill", 0):
                hole.append(pad.drill)
    if hole:
        out["min_through_hole_diameter"] = min(hole)
    return out


def compute_constraint_targets(args, proj: dict, minima: dict):
    """Map KiCad rule keys -> target floor (mm). Routing params win; otherwise
    fall back to the board's smallest object (sizes) or the project's Default
    net-class clearance (copper clearance). Keys absent => leave that rule alone.
    """
    targets = {}

    clearance = args.clearance if args.clearance is not None else project_copper_clearance(proj)
    if clearance is not None:
        targets["min_clearance"] = clearance
    # Hole/copper clearance: explicit flag, else the copper-clearance floor.
    hole_clr = args.hole_clearance if args.hole_clearance is not None else clearance
    if hole_clr is not None:
        targets["min_hole_clearance"] = hole_clr
    if args.hole_to_hole is not None:
        targets["min_hole_to_hole"] = args.hole_to_hole
    if args.edge_clearance is not None:
        targets["min_copper_edge_clearance"] = args.edge_clearance

    # Size minima: routing param if given, else smallest object on the board.
    if args.track_width is not None:
        targets["min_track_width"] = args.track_width
    elif "min_track_width" in minima:
        targets["min_track_width"] = minima["min_track_width"]
    if args.via_size is not None:
        targets["min_via_diameter"] = args.via_size
    elif "min_via_diameter" in minima:
        targets["min_via_diameter"] = minima["min_via_diameter"]
    if args.via_drill is not None:
        targets["min_through_hole_diameter"] = args.via_drill
    elif "min_through_hole_diameter" in minima:
        targets["min_through_hole_diameter"] = minima["min_through_hole_diameter"]
    if "min_via_annular_width" in minima:
        targets["min_via_annular_width"] = minima["min_via_annular_width"]
    return targets


def main():
    ap = argparse.ArgumentParser(
        description="Make a routed board's KiCad DRC settings consistent with the routed floors.")
    ap.add_argument("board", help="Path to the .kicad_pcb (or .kicad_pro) file")
    # Routing parameters (match what you passed route.py); each defaults to the
    # board's own minimum / the project clearance when omitted.
    ap.add_argument("--clearance", type=float, default=None,
                    help="Copper clearance floor in mm (default: project Default net-class clearance)")
    ap.add_argument("--hole-clearance", type=float, default=None,
                    help="Hole/copper clearance floor in mm (default: copper clearance)")
    ap.add_argument("--hole-to-hole", type=float, default=None,
                    help="Hole-to-hole clearance floor in mm (routing --hole-to-hole-clearance)")
    ap.add_argument("--edge-clearance", type=float, default=None,
                    help="Copper-to-edge clearance floor in mm (routing --board-edge-clearance)")
    ap.add_argument("--track-width", type=float, default=None,
                    help="Min track width in mm (default: smallest track on the board)")
    ap.add_argument("--via-size", type=float, default=None,
                    help="Min via diameter in mm (default: smallest via on the board)")
    ap.add_argument("--via-drill", type=float, default=None,
                    help="Min hole/drill diameter in mm (default: smallest drill on the board)")
    ap.add_argument("--keep-courtyards", action="store_true", help="Do not ignore courtyard categories")
    ap.add_argument("--keep-mask", action="store_true", help="Do not ignore solder-mask bridge")
    ap.add_argument("--keep-footprint", action="store_true",
                    help="Do not ignore footprint/library categories (annular_width, lib_footprint_*)")
    ap.add_argument("--keep-thermal", action="store_true",
                    help="Keep starved_thermal as an error (default: demote to a warning)")
    ap.add_argument("--ignore", nargs="+", default=[], metavar="CAT",
                    help="Extra severity categories to set to ignore")
    ap.add_argument("--ignore-warnings", action="store_true",
                    help="Set every category currently at 'warning' severity to 'ignore' "
                         "(hides all warning markers; errors are untouched)")
    ap.add_argument("--dry-run", action="store_true", help="Show changes without writing")
    args = ap.parse_args()

    pro = find_project(args.board)
    if not os.path.isfile(pro):
        sys.exit(f"error: no project file found at {pro}\n"
                 f"  Open the board in KiCad once (it creates the .kicad_pro), then re-run.")

    with open(pro) as f:
        proj = json.load(f)

    ds = proj.setdefault("board", {}).setdefault("design_settings", {})
    rules = ds.setdefault("rules", {})
    sev = ds.setdefault("rule_severities", {})

    # Compute target floors and apply them with "only loosen" semantics: a
    # constraint is lowered toward the routed floor but never raised, so we can
    # never introduce a new violation.
    pcb_path = args.board if args.board.endswith(".kicad_pcb") else os.path.splitext(args.board)[0] + ".kicad_pcb"
    minima = scan_board_minima(pcb_path)
    targets = compute_constraint_targets(args, proj, minima)

    changes = []
    EPS = 1e-9
    for key, target in targets.items():
        if target is None:
            continue
        target = round(float(target), 6)
        cur = rules.get(key)
        # Lower to the target only if the current floor is stricter (higher) or
        # unset; never raise.
        if cur is None or cur > target + EPS:
            changes.append(f"rules.{key}: {cur} -> {target} mm")
            rules[key] = target

    # Keep the Default net class consistent with the copper-clearance floor (and
    # the placed track/via/drill), again only loosening. KiCad enforces the COPPER
    # CLEARANCE per net class (the rules.min_clearance floor alone does not relax
    # it), so leaving the Default class stricter than the board re-introduces the
    # same clearance noise the rules fix removes. Create the Default class if the
    # project has none (a stock template), so the floor actually takes effect.
    nc_map = {"clearance": targets.get("min_clearance"),
              "track_width": targets.get("min_track_width"),
              "via_diameter": targets.get("min_via_diameter"),
              "via_drill": targets.get("min_through_hole_diameter")}
    net_settings = proj.setdefault("net_settings", {})
    net_settings.setdefault("meta", {"version": 0})  # KiCad needs this to read classes
    classes = net_settings.setdefault("classes", [])
    default_cls = next((c for c in classes if c.get("name") == "Default"), None)
    if default_cls is None and any(v is not None for v in nc_map.values()):
        # Seed a COMPLETE class (a sparse stub is ignored by KiCad, which then
        # falls back to the stock 0.2 mm default); the loop below lowers its
        # fields to the routed floor.
        default_cls = dict(_DEFAULT_NETCLASS)
        classes.insert(0, default_cls)
        changes.append("net_class[Default]: created (project had none)")
    if default_cls is not None:
        for field, target in nc_map.items():
            if target is None:
                continue
            target = round(float(target), 6)
            cur = default_cls.get(field)
            if cur is None or cur > target + EPS:
                changes.append(f"net_class[Default].{field}: {cur} -> {target} mm")
                default_cls[field] = target

    # Severities. Only ever LOOSEN (lower the rank error>warning>ignore): a
    # category's severity is changed only if the new level is less strict than
    # the current one (default unset == error), so we never start flagging
    # something the user had silenced.
    def loosen_severity(cat, level):
        cur = sev.get(cat, "error")  # KiCad default severity is "error"
        if _SEV_RANK.get(level, 2) < _SEV_RANK.get(cur, 2):
            changes.append(f"severity[{cat}]: {sev.get(cat)} -> {level}")
            sev[cat] = level

    # -> ignore for non-routing categories.
    to_ignore = list(args.ignore)
    if not args.keep_courtyards:
        to_ignore += COURTYARD_CATS
    if not args.keep_mask:
        to_ignore += MASK_CATS
    if not args.keep_footprint:
        to_ignore += FOOTPRINT_CATS
    if args.ignore_warnings:
        to_ignore += [cat for cat, s in sev.items() if s == "warning"]
    for cat in to_ignore:
        loosen_severity(cat, "ignore")
    # -> warning (demote, still visible) for thermal-relief spoke shortfalls.
    if not args.keep_thermal:
        for cat in WARNING_CATS:
            loosen_severity(cat, "warning")

    if not changes:
        print(f"{pro}: already consistent, nothing to change.")
        return

    print(f"{pro}:")
    for c in changes:
        print(f"  {c}")

    if args.dry_run:
        print("(dry run -- not written)")
        return

    with open(pro, "w") as f:
        json.dump(proj, f, indent=2)
        f.write("\n")
    print(f"\nWrote {pro}. Constraints only loosened toward the routed floor; "
          f"shorts / unconnected are unchanged.")
    print("NOTE: if the board is open in KiCad, close it first and reopen -- "
          "KiCad overwrites the project file on save.")


if __name__ == "__main__":
    main()
