#!/usr/bin/env python3
"""Build the QFN diff-pair under-pad-escape example board for issue #161.

Synthetic repro from the issue reporter (edgehero), cleaned so the *input* is
DRC-clean: a stock HVQFN-32, the pair to escape (DP1) on the outer right edge, a
routed neighbour pair (DP2) whose diagonal escape sweeps into DP1's escape lane,
and a FOREIGN track crossing that lane. The neighbour copper is on the chip's
OWN other nets -- that is what made qfn_fanout's via-drop land a via on a
neighbour's track and short it (issue #161 reopen).

Run with KiCad's bundled python, point LIB at your KiCad footprints dir:
    <kicad-python> tests/build_qfn_diffpair_escape_board.py
Writes kicad_files/qfn_diffpair_escape.kicad_pcb (checked in; the test uses it).
"""
import os
import pcbnew

LIB_CANDIDATES = [
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints/Package_DFN_QFN.pretty",
    "/usr/share/kicad/footprints/Package_DFN_QFN.pretty",
]
FP = "HVQFN-32-1EP_5x5mm_P0.5mm_EP3.1x3.1mm"
LIB = next((p for p in LIB_CANDIDATES if os.path.isdir(p)), LIB_CANDIDATES[0])

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "kicad_files", "qfn_diffpair_escape.kicad_pcb")

b = pcbnew.BOARD()
fp = pcbnew.FootprintLoad(LIB, FP)
fp.SetReference("U1")
fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(11), pcbnew.FromMM(11)))
b.Add(fp)


def net(n):
    ni = pcbnew.NETINFO_ITEM(b, n); b.Add(ni); return ni


N = {k: net(k) for k in ("DP1_P", "DP1_N", "DP2_P", "DP2_N", "FOREIGN")}

pads = list(fp.Pads())
maxx = max(p.GetPosition().x for p in pads)
miny = min(p.GetPosition().y for p in pads)
right = sorted([p for p in pads if abs(p.GetPosition().x - maxx) < pcbnew.FromMM(0.3)],
               key=lambda p: p.GetPosition().y)
top = sorted([p for p in pads if abs(p.GetPosition().y - miny) < pcbnew.FromMM(0.3)],
             key=lambda p: p.GetPosition().x)
right[0].SetNet(N["DP1_N"]); right[1].SetNet(N["DP1_P"])   # outermost pair = the stuck one
right[2].SetNet(N["DP2_N"]); right[3].SetNet(N["DP2_P"])   # routed neighbour
top[0].SetNet(N["FOREIGN"]); top[1].SetNet(N["FOREIGN"])   # anchor FOREIGN so the net survives


def trk(name, x0, y0, x1, y1):
    t = pcbnew.PCB_TRACK(b)
    t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x0), pcbnew.FromMM(y0)))
    t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(x1), pcbnew.FromMM(y1)))
    t.SetWidth(pcbnew.FromMM(0.127)); t.SetLayer(pcbnew.F_Cu); t.SetNet(N[name]); b.Add(t)


# Neighbour pair routed diagonally outward, sweeping into DP1's escape lane.
trk("DP2_N", 13.44, 10.25, 15.6, 9.9)
trk("DP2_P", 13.44, 10.75, 15.6, 10.4)
# Foreign track across DP1's outward escape lane, clear of all pads (right pads
# end at x~13.875) and well clear of the DP2 tracks (y >= 9.9). It boxes in the
# outward side so a via that staggers outward would land on it -- the via must
# stagger inward instead.
trk("FOREIGN", 14.40, 9.55, 16.50, 9.55)

for x0, y0, x1, y1 in [(1, 1, 21, 1), (21, 1, 21, 21), (21, 21, 1, 21), (1, 21, 1, 1)]:
    s = pcbnew.PCB_SHAPE(b); s.SetShape(pcbnew.SHAPE_T_SEGMENT)
    s.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x0), pcbnew.FromMM(y0)))
    s.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(x1), pcbnew.FromMM(y1)))
    s.SetLayer(pcbnew.Edge_Cuts); b.Add(s)

pcbnew.SaveBoard(OUT, b)
print(f"wrote {OUT}")
