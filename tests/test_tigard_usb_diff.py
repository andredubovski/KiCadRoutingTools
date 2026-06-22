#!/usr/bin/env python3
"""
Real-world differential-pair test: the USB 2.0 pair (/USB_DP, /USB_DN) of the
Tigard board (`kicad_files/tigard.kicad_pcb`, a QFN-64 FT2232H + USB-C connector).

This is the board that drove the issue #165 work. The pair is a fine-pitch
connector fan-out: J1 has redundant DP/DN pads (USB-C flip support) at 0.5mm
pitch, and U3.7/U3.8 sit on the congested QFN edge. Before #165, route_diff
either grazed the partner pad, floated on an inner layer, or deferred the whole
pair to single-ended. It now:

  1. coupled-routes the U3<->J1 leg directly (escape-direction + pad-edge launch
     fixes), and
  2. a single-ended follow-up connects the redundant J1 row,

leaving the pair FULLY CONNECTED and DRC-clean. This test guards that end to end.

Run:
    python3 tests/test_tigard_usb_diff.py
    python3 tests/test_tigard_usb_diff.py -v
"""

import argparse
import os
import subprocess
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)

BOARD = os.path.join(ROOT_DIR, "kicad_files", "tigard.kicad_pcb")
NETS = ["/USB_DP", "/USB_DN"]
CLEARANCE = "0.15"
TRACK = "0.205232"
LAYERS = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
DIFF_GEOM = ["--track-width", TRACK, "--diff-pair-gap", "0.2032",
             "--clearance", CLEARANCE, "--layers", *LAYERS, "--no-gnd-vias"]
SE_GEOM = ["--clearance", CLEARANCE, "--track-width", TRACK,
           "--via-size", "0.5", "--via-drill", "0.25", "--grid-step", "0.05",
           "--no-bga-zone", "--layers", *LAYERS, "--max-ripup", "10"]


def _run(args, verbose):
    r = subprocess.run([sys.executable, *args], cwd=ROOT_DIR,
                       capture_output=True, text=True)
    txt = r.stdout + r.stderr
    if verbose:
        print(txt)
    return txt


def _drc_clean(board, verbose):
    txt = _run(["check_drc.py", board, "--clearance", CLEARANCE,
                "--nets", *NETS, "--clearance-margin", "0.1"], verbose)
    return "NO DRC VIOLATIONS" in txt


def _connected(board, verbose):
    txt = _run(["check_connected.py", board, "--nets", *NETS], verbose)
    return "ALL NETS FULLY CONNECTED" in txt


def main():
    ap = argparse.ArgumentParser(description="Tigard USB diff-pair routing test")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    v = args.verbose

    results = []

    def check(name, ok, detail=""):
        results.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")

    if not os.path.exists(BOARD):
        print(f"Board not found: {BOARD}")
        return 1

    tmp = []

    def _tmp(prefix):
        fd, p = tempfile.mkstemp(suffix=".kicad_pcb", prefix=prefix)
        os.close(fd)
        tmp.append(p)
        return p

    try:
        # Step 1: route_diff must COUPLED-route the pair (not defer it).
        diff_out = _tmp("tigard_diff_")
        txt = _run(["route_diff.py", BOARD, diff_out, "--nets", *NETS, *DIFF_GEOM], v)
        coupled = '"routed_diff_pairs": ["/USB_D"]' in txt and '"failed": 0' in txt
        check("route_diff coupled-routes the USB pair (not deferred/failed)", coupled,
              "" if coupled else "pair was deferred to single-ended or failed")
        check("coupled route is DRC-clean", _drc_clean(diff_out, v))

        # Step 2: single-ended follow-up connects the redundant J1 row.
        final_out = _tmp("tigard_final_")
        _run(["route.py", diff_out, final_out, "--nets", *NETS, *SE_GEOM], v)
        check("USB pair fully connected after single-ended follow-up",
              _connected(final_out, v))
        check("final board is DRC-clean", _drc_clean(final_out, v))
    finally:
        for p in tmp:
            if os.path.exists(p):
                os.remove(p)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"  {passed}/{total} checks passed")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
