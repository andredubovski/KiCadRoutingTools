#!/usr/bin/env python3
"""Issue #160 auto-invoke: fix_project_for_output() ensures a routed board has a
sibling .kicad_pro consistent with the routed floors -- copying the input
board's project when the output is a new file, or seeding a complete one when the
input has none -- and never touches the .kicad_pcb.
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fix_kicad_drc_settings import fix_project_for_output

BOARD = os.path.join(ROOT, "kicad_files", "qfn_underpad_coupling.kicad_pcb")
PARAMS = dict(clearance=0.15, track_width=0.15, via_diameter=0.4, via_drill=0.3,
              hole_to_hole=0.2, edge_clearance=0.0)


def _md5(p):
    return hashlib.md5(open(p, "rb").read()).hexdigest()


def main():
    if not os.path.exists(BOARD):
        print(f"FAIL: test board missing ({BOARD})")
        return 1
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        # (1) No input project -> seed a complete project at the output floors.
        out = os.path.join(tmp, "routed.kicad_pcb")
        shutil.copyfile(BOARD, out)
        md5_before = _md5(out)
        fix_project_for_output(out, input_pcb=None, verbose=False, **PARAMS)
        pro = os.path.join(tmp, "routed.kicad_pro")
        if not os.path.isfile(pro):
            fails.append("seed: no .kicad_pro was created")
        else:
            p = json.load(open(pro))
            r = p["board"]["design_settings"]["rules"]
            if abs(r.get("min_clearance", -1) - 0.15) > 1e-9:
                fails.append(f"seed: min_clearance = {r.get('min_clearance')}")
            default = next((c for c in p["net_settings"]["classes"] if c.get("name") == "Default"), None)
            if default is None or abs(default.get("clearance", -1) - 0.15) > 1e-9:
                fails.append("seed: Default net class clearance not 0.15")
            if "priority" not in (default or {}):
                fails.append("seed: created Default class is not complete")
            if p["board"]["design_settings"]["rule_severities"].get("starved_thermal") != "warning":
                fails.append("seed: starved_thermal not demoted to warning")
        if _md5(out) != md5_before:
            fails.append("seed: the .kicad_pcb was modified")

        # (2) Output is a new file and the INPUT has a project -> carry it over
        # (preserving the user's settings) and then loosen.
        in_pcb = os.path.join(tmp, "src.kicad_pcb")
        in_pro = os.path.join(tmp, "src.kicad_pro")
        shutil.copyfile(BOARD, in_pcb)
        json.dump({"board": {"design_settings": {
            "rules": {"min_clearance": 0.25},
            "rule_severities": {}}},
            "net_settings": {"classes": [{"name": "Default", "clearance": 0.25,
                                          "priority": 2147483647}], "meta": {"version": 0}},
            "meta": {"version": 1}, "i_am_the_input": True}, open(in_pro, "w"))
        out2 = os.path.join(tmp, "src_routed.kicad_pcb")
        shutil.copyfile(BOARD, out2)
        fix_project_for_output(out2, input_pcb=in_pcb, verbose=False, **PARAMS)
        pro2 = os.path.join(tmp, "src_routed.kicad_pro")
        if not os.path.isfile(pro2):
            fails.append("copy: no output .kicad_pro created")
        else:
            p2 = json.load(open(pro2))
            if not p2.get("i_am_the_input"):
                fails.append("copy: did not carry over the input project")
            r2 = p2["board"]["design_settings"]["rules"]
            if abs(r2.get("min_clearance", -1) - 0.15) > 1e-9:
                fails.append(f"copy: min_clearance not loosened to 0.15 (got {r2.get('min_clearance')})")
            d2 = next((c for c in p2["net_settings"]["classes"] if c.get("name") == "Default"), None)
            if d2 is None or abs(d2.get("clearance", -1) - 0.15) > 1e-9:
                fails.append("copy: Default class clearance not loosened to 0.15")

    if fails:
        print("FAIL: " + "; ".join(fails))
        return 1
    print("PASS: seeds a complete project when none exists, carries over & loosens "
          "the input project otherwise, .kicad_pcb untouched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
