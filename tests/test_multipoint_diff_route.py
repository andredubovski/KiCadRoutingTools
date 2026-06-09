#!/usr/bin/env python3
"""
Pass/fail test for bare-pad and multi-point differential-pair routing.

Board: `kicad_files/lvds_converter_dualclk.kicad_pcb` - a simple 2-layer LVDS
converter where the diff pairs terminate directly on bare SMD/PTH pads (no
fanout stubs) at MULTIPLE pad-pair terminals:

- /DATA+ / /DATA-  : 3 terminals (J1 connector, IC4 pins 10/9, R4 termination)
- /CLK+  / /CLK-   : 4 terminals (J1, R3 termination, IC4 pins 2/1 AND 7/6)

This exercises, end to end:
- suffix-style-aware pair detection (/CLK+ pairs with /CLK-, not /CLK_N)
- synthesized escape directions for bare-pad endpoints
- multi-point chain routing (legs passing "through" shared terminals on
  opposite sides, alternative chain orderings on failure)
- per-leg geometric polarity resolution (no pad swaps)

Each scenario asserts the pair(s) routed, EVERY terminal is connected
(multi-point connectivity), no pad nets were swapped, and the result is
DRC-clean scoped to the pair nets. Geometry matches the board's netclass
(0.2mm track / 0.25mm pair gap / 0.2mm clearance).

Run:
    python3 tests/test_multipoint_diff_route.py
    python3 tests/test_multipoint_diff_route.py -v   # keep routing output
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)

BOARD = os.path.join(ROOT_DIR, "kicad_files", "lvds_converter_dualclk.kicad_pcb")
# Match the board's netclass: 0.2mm track, 0.25mm pair gap, 0.2mm clearance
GEOM = ["--track-width", "0.2", "--diff-pair-gap", "0.25", "--clearance", "0.2",
        "--layers", "F.Cu", "B.Cu"]
CLEARANCE = "0.2"

# (name, sequential routing runs, expected legs markers across all runs)
# Each run is (nets, expected routed pairs); later runs route on the previous
# run's output. CLK (4 terminals) routes before DATA: the more-constrained
# pair gets first pick of the space around the shared J1 connector.
SCENARIOS = [
    ("DATA multi-point (3 terminals, 2 legs)",
     [(["/DATA+", "/DATA-"], 1)], {"3 terminals, 2 legs": 1}),
    ("CLK multi-point (4 terminals, 3 legs)",
     [(["/CLK+", "/CLK-"], 1)], {"4 terminals, 3 legs": 1}),
    ("CLK then DATA (sequential runs)",
     [(["/CLK+", "/CLK-"], 1), (["/DATA+", "/DATA-"], 1)],
     {"4 terminals, 3 legs": 1, "3 terminals, 2 legs": 1}),
]


def route(board, nets, out):
    """Route the given nets on *board*; return (summary_dict_or_None, output_text)."""
    cmd = [sys.executable, "route_diff.py", board, out, "--nets"] + nets + GEOM
    r = subprocess.run(cmd, cwd=ROOT_DIR, capture_output=True, text=True)
    txt = r.stdout + r.stderr
    m = re.search(r"JSON_SUMMARY: (\{.*\})", txt)
    return (json.loads(m.group(1)) if m else None), txt


def is_connected(board, nets):
    """True if check_connected reports ALL the pair nets fully connected
    (for multi-point pairs this requires every terminal to be reached)."""
    cmd = [sys.executable, "check_connected.py", board, "--nets"] + nets
    r = subprocess.run(cmd, cwd=ROOT_DIR, capture_output=True, text=True)
    return "ALL NETS FULLY CONNECTED" in (r.stdout + r.stderr)


def drc_clean(board, nets):
    """True if the routed nets have no DRC violations (scoped to themselves)."""
    cmd = [sys.executable, "check_drc.py", board, "--clearance", CLEARANCE,
           "--nets"] + nets
    r = subprocess.run(cmd, cwd=ROOT_DIR, capture_output=True, text=True)
    return "NO DRC VIOLATIONS" in (r.stdout + r.stderr)


def scenario(name, runs, expect_legs, verbose):
    log = []
    outs = []
    try:
        board = BOARD
        all_nets = []
        routed_ok = True
        no_swaps = True
        all_txt = ""
        for nets, expect_routed in runs:
            fd, out = tempfile.mkstemp(suffix=".kicad_pcb", prefix="mpdiff_")
            os.close(fd)
            outs.append(out)
            summary, txt = route(board, nets, out)
            if verbose:
                print(txt)
            if summary is None or not os.path.exists(out):
                return name, False, ["route_diff produced no summary/output"]
            all_txt += txt
            all_nets += nets
            routed_ok = routed_ok and (summary.get("successful") == expect_routed
                                       and summary.get("failed") == 0)
            log.append(f"routed {','.join(nets)}: {summary.get('successful')}/{expect_routed} "
                       f"failed={summary.get('failed')}")
            # Multi-point pairs must never swap pad nets (a swap at a shared
            # terminal would break the other leg)
            no_swaps = no_swaps and (summary.get("polarity_swapped_pairs") == []
                                     and summary.get("target_swaps") == [])
            board = out  # next run routes on this output

        log.append(f"no_pad_swaps={no_swaps}")

        legs_ok = True
        for marker, count in expect_legs.items():
            found = all_txt.count(f"Multi-point pair: {marker}")
            if found != count:
                legs_ok = False
                log.append(f"expected {count}x 'Multi-point pair: {marker}', got {found}")
        if legs_ok:
            log.append("multi-point leg plan as expected")

        final = outs[-1]
        conn = is_connected(final, all_nets)
        clean = drc_clean(final, all_nets)
        log.append(f"connected(all terminals)={conn}  drc_clean(pair-scoped)={clean}")

        return name, (routed_ok and no_swaps and legs_ok and conn and clean), log
    finally:
        for out in outs:
            if os.path.exists(out):
                os.remove(out)


def main():
    parser = argparse.ArgumentParser(
        description="Bare-pad / multi-point differential-pair routing test")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show routing output")
    args = parser.parse_args()

    if not os.path.exists(BOARD):
        print(f"ERROR: board not found: {BOARD}")
        return 2

    print("=" * 70)
    print("Bare-pad / multi-point differential-pair routing test")
    print("=" * 70)

    results = []
    for name, runs, expect_legs in SCENARIOS:
        sname, passed, log = scenario(name, runs, expect_legs, args.verbose)
        print(f"\n[{'PASS' if passed else 'FAIL'}] {sname}")
        for line in log:
            print(f"        {line}")
        results.append((sname, passed))

    print("\n" + "=" * 70)
    n_pass = sum(1 for _, p in results if p)
    for sname, passed in results:
        print(f"  {'PASS' if passed else 'FAIL'}  {sname}")
    print(f"\n{n_pass}/{len(results)} scenarios passed")
    print("=" * 70)
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
