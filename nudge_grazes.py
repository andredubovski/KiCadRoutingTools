#!/usr/bin/env python3
"""Post-route clearance nudge (issue #70).

The routing grid leaves sub-cell clearance *grazes*: a track threading
diagonally between cells, or an obstacle halo floored by part of a cell, ends
up a hair (sub-micron .. up to ~half a cell) inside the required clearance.
Rather than pessimise the router with a half-cell safety buffer (which costs
routing density on every board), fix the geometry AFTER routing.

Every routed track endpoint / via centre is a movable joint; two pieces of
copper are electrically joined where they share such a point. So if we shift a
joint and move EVERY piece of copper that shares it together, the connection is
unchanged -- only the geometry moves. The offending copper is shifted
perpendicular away from the obstacle just enough to satisfy clearance.

Two invariants make this safe:
  * Every move is <= 1/2 grid step. A graze is a sub-cell artefact, so the fix
    is sub-cell too; moving farther would push copper into space the router
    never validated as clear. Overlaps larger than 1/2 step are real congestion
    and are left alone.
  * Every move is validated and rolled back unless it STRICTLY reduces the total
    other-net overlap of the copper it touched -- so a nudge can never create
    (or worsen) a violation elsewhere, and the pass is guaranteed to terminate.

Pad-terminal joints (a track landing on its pad) are anchored and never moved,
so pad connections are preserved.

CLI:
    python3 nudge_grazes.py in.kicad_pcb out.kicad_pcb --clearance 0.15 --grid-step 0.05
"""

import argparse
import math
import re
from collections import defaultdict
from typing import Dict, List, Tuple

from kicad_parser import parse_kicad_pcb
from geometry_utils import closest_point_on_segment, point_to_segment_distance
from check_drc import segment_to_rect_distance, expand_pad_layers


def _key(x: float, y: float) -> Tuple[int, int]:
    # Routed copper meets at identical mm coords (written from the same integer-nm
    # point); rounding to 1 nm groups true joints without merging distinct points.
    return (round(x * 1e6), round(y * 1e6))


def _pad_corner_radius(pad) -> float:
    if pad.shape in ('circle', 'oval'):
        return min(pad.size_x, pad.size_y) / 2
    if pad.shape == 'roundrect':
        return pad.roundrect_rratio * min(pad.size_x, pad.size_y)
    return 0.0


def _point_in_pad(x: float, y: float, pad) -> bool:
    dx, dy = x - pad.global_x, y - pad.global_y
    if pad.rect_rotation:
        rad = math.radians(pad.rect_rotation)
        c, s = math.cos(rad), math.sin(rad)
        dx, dy = dx * c + dy * s, -dx * s + dy * c
    return abs(dx) <= pad.size_x / 2 and abs(dy) <= pad.size_y / 2


class _Pad:
    __slots__ = ('cx', 'cy', 'hw', 'hh', 'cr', 'net', 'layers')

    def __init__(self, pad, routing_layers):
        self.cx, self.cy = pad.global_x, pad.global_y
        self.hw, self.hh = pad.size_x / 2, pad.size_y / 2
        self.cr = _pad_corner_radius(pad)
        self.net = pad.net_id
        self.layers = set(expand_pad_layers(pad.layers, routing_layers))

    def dist_to_seg(self, sx, sy, ex, ey):
        d, _ = segment_to_rect_distance(sx, sy, ex, ey, self.cx, self.cy, self.hw, self.hh, self.cr)
        return d

    def dist_to_pt(self, x, y):
        d, _ = segment_to_rect_distance(x, y, x, y, self.cx, self.cy, self.hw, self.hh, self.cr)
        return d


class _Model:
    """Mutable routed-copper geometry over which we nudge."""

    def __init__(self, pcb, clearance, grid_step):
        self.clr = clearance
        self.grid = grid_step
        self.max_move = grid_step / 2.0
        self.buffer = min(grid_step * 0.02, self.max_move * 0.1)
        rl = pcb.board_info.copper_layers or ['F.Cu', 'B.Cu']
        self.net_name = {nid: n.name for nid, n in pcb.nets.items()}

        self.seg = [[s.start_x, s.start_y, s.end_x, s.end_y] for s in pcb.segments]
        self.seg_orig = [tuple(c) for c in self.seg]   # None for jog-inserted segments
        self.sw = [s.width for s in pcb.segments]
        self.slayer = [s.layer for s in pcb.segments]
        self.snet = [s.net_id for s in pcb.segments]
        self.alive = [True] * len(self.seg)

        self.via = [[v.x, v.y] for v in pcb.vias]
        self.via_orig = [tuple(c) for c in self.via]
        self.vsize = [v.size for v in pcb.vias]
        self.vnet = [v.net_id for v in pcb.vias]

        self.pads = [_Pad(p, rl) for pads in pcb.pads_by_net.values() for p in pads]
        self._raw_pads = [p for pads in pcb.pads_by_net.values() for p in pads]
        self.pads_by_net = pcb.pads_by_net

        self.cell = max(1.0, 4 * clearance)

    # ---- spatial index over model indices (live coords read on query) ----
    def build_index(self):
        seg_cells: Dict[Tuple, list] = defaultdict(list)   # (layer,cx,cy) -> [seg idx]
        via_cells: Dict[Tuple, list] = defaultdict(list)   # (cx,cy) -> [via idx]
        pad_cells: Dict[Tuple, list] = defaultdict(list)   # (layer,cx,cy) -> [pad idx]
        cs = self.cell
        pad_margin = self.clr + self.max_move
        for i, (sx, sy, ex, ey) in enumerate(self.seg):
            if not self.alive[i]:
                continue
            for c in self._bbox_cells(sx, sy, ex, ey, self.sw[i] / 2 + self.clr + self.max_move):
                seg_cells[(self.slayer[i],) + c].append(i)
        for i, (x, y) in enumerate(self.via):
            for c in self._bbox_cells(x, y, x, y, self.vsize[i] / 2 + self.clr + self.max_move):
                via_cells[c].append(i)
        for i, p in enumerate(self.pads):
            ext = max(p.hw, p.hh) + pad_margin
            for c in self._bbox_cells(p.cx, p.cy, p.cx, p.cy, ext):
                for layer in p.layers:
                    pad_cells[(layer,) + c].append(i)
        self.seg_cells, self.via_cells, self.pad_cells = seg_cells, via_cells, pad_cells

    def _bbox_cells(self, x1, y1, x2, y2, pad):
        cs = self.cell
        cx0 = int(math.floor((min(x1, x2) - pad) / cs))
        cx1 = int(math.floor((max(x1, x2) + pad) / cs))
        cy0 = int(math.floor((min(y1, y2) - pad) / cs))
        cy1 = int(math.floor((max(y1, y2) + pad) / cs))
        return [(gx, gy) for gx in range(cx0, cx1 + 1) for gy in range(cy0, cy1 + 1)]

    def _cands(self, table, keyprefix, x1, y1, x2, y2, pad):
        seen = set()
        out = []
        for c in self._bbox_cells(x1, y1, x2, y2, pad):
            for idx in table.get(keyprefix + c, ()):
                if idx not in seen:
                    seen.add(idx)
                    out.append(idx)
        return out

    # ---- coincidence groups + pad anchors ----
    def add_segment(self, sx, sy, ex, ey, width, layer, net):
        """Append a jog-inserted segment (orig=None marks it as 'added')."""
        self.seg.append([sx, sy, ex, ey])
        self.seg_orig.append(None)
        self.sw.append(width)
        self.slayer.append(layer)
        self.snet.append(net)
        self.alive.append(True)

    def build_groups(self):
        groups = defaultdict(list)
        for i, (sx, sy, ex, ey) in enumerate(self.seg):
            if not self.alive[i]:
                continue
            groups[_key(sx, sy)].append(('s', i, 0))
            groups[_key(ex, ey)].append(('s', i, 1))
        for i, (x, y) in enumerate(self.via):
            groups[_key(x, y)].append(('v', i, -1))
        anchored = set()
        for k, members in groups.items():
            x, y = k[0] / 1e6, k[1] / 1e6
            for kind, idx, _ in members:
                net = self.snet[idx] if kind == 's' else self.vnet[idx]
                if any(_point_in_pad(x, y, p) for p in self.pads_by_net.get(net, [])):
                    anchored.add(k)
                    break
        self.groups, self.anchored = groups, anchored

    def movable(self, x, y):
        return _key(x, y) not in self.anchored

    def members(self, x, y):
        return self.groups.get(_key(x, y), [])

    def shift(self, x, y, dx, dy):
        for kind, idx, end in self.groups.get(_key(x, y), ()):
            if kind == 's':
                b = 0 if end == 0 else 2
                self.seg[idx][b] += dx
                self.seg[idx][b + 1] += dy
            else:
                self.via[idx][0] += dx
                self.via[idx][1] += dy

    # ---- overlap of one piece against nearby OTHER-net copper (live coords) ----
    def seg_overlap_sum(self, i):
        sx, sy, ex, ey = self.seg[i]
        return self.seg_overlap_at(sx, sy, ex, ey, self.sw[i], self.slayer[i], self.snet[i], skip=i)

    def seg_overlap_at(self, sx, sy, ex, ey, w, layer, net, skip=None, count=False, thresh=0.0):
        """Other-net clearance overlap for a segment of the given geometry.
        count=False -> sum of overlap depths; count=True -> number of violating
        pairs whose overlap exceeds `thresh` (thresh=0 counts every violation;
        thresh=max_move counts only genuine >1/2-step ones). Used to validate
        jogs, which must never raise the violation count (or the genuine count)."""
        half = w / 2
        pad = half + self.clr + self.max_move
        total = 0.0

        def add(ov):
            nonlocal total
            if count:
                if ov > thresh:
                    total += 1
            else:
                total += ov

        for j in self._cands(self.seg_cells, (layer,), sx, sy, ex, ey, pad):
            if j == skip or not self.alive[j] or self.snet[j] == net:
                continue
            ox, oy, oex, oey = self.seg[j]
            d = _seg_seg_dist(sx, sy, ex, ey, ox, oy, oex, oey)
            req = half + self.sw[j] / 2 + self.clr
            if d < req:
                add(req - d)
        for j in self._cands(self.via_cells, (), sx, sy, ex, ey, pad):
            if self.vnet[j] == net:
                continue
            vx, vy = self.via[j]
            d = point_to_segment_distance(vx, vy, sx, sy, ex, ey)
            req = self.vsize[j] / 2 + half + self.clr
            if d < req:
                add(req - d)
        for j in self._cands(self.pad_cells, (layer,), sx, sy, ex, ey, pad):
            p = self.pads[j]
            if p.net == net:
                continue
            d = p.dist_to_seg(sx, sy, ex, ey)
            req = half + self.clr
            if d < req:
                add(req - d)
        return total

    def via_overlap_sum(self, i):
        vx, vy = self.via[i]
        r, net = self.vsize[i] / 2, self.vnet[i]
        pad = r + self.clr + self.max_move
        total = 0.0
        for j in self._cands(self.via_cells, (), vx, vy, vx, vy, pad):
            if j == i or self.vnet[j] == net:
                continue
            ox, oy = self.via[j]
            d = math.hypot(vx - ox, vy - oy)
            req = r + self.vsize[j] / 2 + self.clr
            if d < req:
                total += req - d
        # vias block all copper layers -> check segments/pads on every layer
        for j in self._all_layer_seg_cands(vx, vy, pad):
            if not self.alive[j] or self.snet[j] == net:
                continue
            sx, sy, ex, ey = self.seg[j]
            d = point_to_segment_distance(vx, vy, sx, sy, ex, ey)
            req = r + self.sw[j] / 2 + self.clr
            if d < req:
                total += req - d
        for j in self._all_layer_pad_cands(vx, vy, pad):
            p = self.pads[j]
            if p.net == net:
                continue
            d = p.dist_to_pt(vx, vy)
            req = r + self.clr
            if d < req:
                total += req - d
        return total

    def _all_layer_seg_cands(self, x, y, pad):
        # vias block every copper layer, so gather segment candidates across layers.
        seen = set()
        out = []
        for c in self._bbox_cells(x, y, x, y, pad):
            for layer in self._layers_present_seg:
                for j in self.seg_cells.get((layer,) + c, ()):
                    if j not in seen:
                        seen.add(j)
                        out.append(j)
        return out

    def _all_layer_pad_cands(self, x, y, pad):
        seen = set()
        out = []
        for c in self._bbox_cells(x, y, x, y, pad):
            for layer in self._layers_present_pad:
                for j in self.pad_cells.get((layer,) + c, ()):
                    if j not in seen:
                        seen.add(j)
                        out.append(j)
        return out


def _seg_seg_dist(ax, ay, bx, by, cx, cy, dx, dy):
    # minimal segment-segment distance (no closest points needed for the sum)
    if _seg_cross(ax, ay, bx, by, cx, cy, dx, dy):
        return 0.0
    return min(
        point_to_segment_distance(ax, ay, cx, cy, dx, dy),
        point_to_segment_distance(bx, by, cx, cy, dx, dy),
        point_to_segment_distance(cx, cy, ax, ay, bx, by),
        point_to_segment_distance(dx, dy, ax, ay, bx, by),
    )


def _seg_cross(ax, ay, bx, by, cx, cy, dx, dy):
    def ccw(px, py, qx, qy, rx, ry):
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)
    d1 = ccw(cx, cy, dx, dy, ax, ay)
    d2 = ccw(cx, cy, dx, dy, bx, by)
    d3 = ccw(ax, ay, bx, by, cx, cy)
    d4 = ccw(ax, ay, bx, by, dx, dy)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _unit_away(fromp, top):
    ux, uy = top[0] - fromp[0], top[1] - fromp[1]
    d = math.hypot(ux, uy)
    if d < 1e-9:
        return None
    return ux / d, uy / d


def _nudge_pass(m: _Model):
    """One pass over all grazes; returns number of accepted moves."""
    m.build_index()
    m.build_groups()
    m._layers_present_seg = set(m.slayer)
    m._layers_present_pad = set().union(*[p.layers for p in m.pads]) if m.pads else set()
    moved = set()
    accepted = 0
    cap = m.max_move

    def atomic(shifts, touched):
        """shifts: list of (x,y,dx,dy). touched: list of ('s'/'v', idx). Keep iff
        it strictly lowers touched pieces' total overlap; else roll back."""
        nonlocal accepted
        keys = [_key(x, y) for (x, y, _, _) in shifts]
        if any(k in m.anchored or k in moved for k in keys):
            return False
        if any(math.hypot(dx, dy) > cap + 1e-12 for (_, _, dx, dy) in shifts):
            return False
        pre = sum(m.seg_overlap_sum(i) if t == 's' else m.via_overlap_sum(i) for t, i in touched)
        for (x, y, dx, dy) in shifts:
            m.shift(x, y, dx, dy)
        post = sum(m.seg_overlap_sum(i) if t == 's' else m.via_overlap_sum(i) for t, i in touched)
        if post < pre - 1e-9:
            for k in keys:
                moved.add(k)
            accepted += 1
            return True
        for (x, y, dx, dy) in shifts:   # rollback (groups still keyed by ORIGINAL coords)
            m.shift(x, y, -dx, -dy)
        return False

    def touched_of(keys):
        out = []
        for k in keys:
            for kind, idx, _ in m.groups.get(k, ()):
                out.append((kind, idx))
        return out

    def move_seg(i, ux, uy, overlap):
        amt = min(overlap + m.buffer, cap)
        sx, sy, ex, ey = m.seg[i]
        sk, ek = _key(sx, sy), _key(ex, ey)
        s_ok = m.movable(sx, sy) and sk not in moved
        e_ok = m.movable(ex, ey) and ek not in moved
        dx, dy = ux * amt, uy * amt
        if s_ok and e_ok and sk != ek:
            sh = [(sx, sy, dx, dy), (ex, ey, dx, dy)]
        elif s_ok:
            sh = [(sx, sy, dx, dy)]
        elif e_ok:
            sh = [(ex, ey, dx, dy)]
        else:
            return False
        return atomic(sh, touched_of([_key(x, y) for (x, y, _, _) in sh]))

    def try_jog(i, ux, uy, overlap, actual, required, cpt):
        """When an endpoint can't move (anchored to a pad), insert ONE new vertex
        at the overlap point and offset it away from the obstacle, splitting the
        track A-B into A-V'-B. Both original (anchored) ends stay put."""
        nonlocal accepted
        if not m.alive[i]:
            return False
        sx, sy, ex, ey = m.seg[i]
        w, layer, net = m.sw[i], m.slayer[i], m.snet[i]
        seglen = math.hypot(ex - sx, ey - sy)
        if seglen < 4 * m.grid:
            return False
        tx, ty = (ex - sx) / seglen, (ey - sy) / seglen
        s_c = (cpt[0] - sx) * tx + (cpt[1] - sy) * ty   # overlap point along A->B
        if s_c <= m.grid or s_c >= seglen - m.grid:      # too close to an end -> skip
            return False
        amt = min(overlap + m.buffer, cap)
        vx, vy = cpt[0] + ux * amt, cpt[1] + uy * amt    # offset vertex
        # Validate on violation COUNT: a jog may only REMOVE violations, never
        # split one graze into two still-counted ones.
        pre = m.seg_overlap_at(sx, sy, ex, ey, w, layer, net, skip=i, count=True)
        post = (m.seg_overlap_at(sx, sy, vx, vy, w, layer, net, skip=i, count=True)
                + m.seg_overlap_at(vx, vy, ex, ey, w, layer, net, skip=i, count=True))
        if post < pre - 1e-9:
            m.alive[i] = False
            m.add_segment(sx, sy, vx, vy, w, layer, net)
            m.add_segment(vx, vy, ex, ey, w, layer, net)
            accepted += 1
            # Refresh the index/groups so later moves+jogs in THIS pass see the
            # new geometry (the new vertex is not in the pass-start index).
            m.build_index()
            m.build_groups()
            m._layers_present_seg = set(m.slayer)
            return True
        return False

    def fix_seg(i, ux, uy, ov, actual, required, cpt):
        if not move_seg(i, ux, uy, ov) and m.jog:
            try_jog(i, ux, uy, ov, actual, required, cpt)

    def move_via(i, ux, uy, overlap):
        amt = min(overlap + m.buffer, cap)
        vx, vy = m.via[i]
        if not m.movable(vx, vy):
            return
        atomic([(vx, vy, ux * amt, uy * amt)], touched_of([_key(vx, vy)]))

    # --- segment victims ---
    n_seg0 = len(m.seg)
    for i in range(n_seg0):
        if not m.alive[i]:
            continue
        sx, sy, ex, ey = m.seg[i]
        w, layer, net = m.sw[i], m.slayer[i], m.snet[i]
        half = w / 2
        pad = half + m.clr + cap
        # vs segments
        for j in m._cands(m.seg_cells, (layer,), sx, sy, ex, ey, pad):
            if j == i or not m.alive[j] or m.snet[j] == net:
                continue
            ox, oy, oex, oey = m.seg[j]
            d = _seg_seg_dist(sx, sy, ex, ey, ox, oy, oex, oey)
            ov = (half + m.sw[j] / 2 + m.clr) - d
            if 1e-6 < ov <= cap:
                cpt = closest_point_on_segment((ox + oex) / 2, (oy + oey) / 2, sx, sy, ex, ey)
                ocp = closest_point_on_segment(cpt[0], cpt[1], ox, oy, oex, oey)
                u = _unit_away(ocp, cpt)
                if u:
                    fix_seg(i, u[0], u[1], ov, d, half + m.sw[j] / 2 + m.clr, cpt)
                    if not m.alive[i]:
                        break
                    sx, sy, ex, ey = m.seg[i]
        if not m.alive[i]:
            continue
        # vs vias
        for j in m._cands(m.via_cells, (), sx, sy, ex, ey, pad):
            if m.vnet[j] == net:
                continue
            vx, vy = m.via[j]
            d = point_to_segment_distance(vx, vy, sx, sy, ex, ey)
            ov = (m.vsize[j] / 2 + half + m.clr) - d
            if 1e-6 < ov <= cap:
                cpt = closest_point_on_segment(vx, vy, sx, sy, ex, ey)
                u = _unit_away((vx, vy), cpt)
                if u:
                    fix_seg(i, u[0], u[1], ov, d, m.vsize[j] / 2 + half + m.clr, cpt)
                    if not m.alive[i]:
                        break
                    sx, sy, ex, ey = m.seg[i]
        if not m.alive[i]:
            continue
        # vs pads
        for j in m._cands(m.pad_cells, (layer,), sx, sy, ex, ey, pad):
            p = m.pads[j]
            if p.net == net:
                continue
            d, cpt = segment_to_rect_distance(sx, sy, ex, ey, p.cx, p.cy, p.hw, p.hh, p.cr)
            ov = (half + m.clr) - d
            if 1e-6 < ov <= cap and cpt:
                u = _unit_away((p.cx, p.cy), cpt)
                if u:
                    fix_seg(i, u[0], u[1], ov, d, half + m.clr, cpt)
                    if not m.alive[i]:
                        break
                    sx, sy, ex, ey = m.seg[i]

    # --- via victims (vs vias and pads; via-seg is handled as seg victim) ---
    for i in range(len(m.via)):
        vx, vy = m.via[i]
        r, net = m.vsize[i] / 2, m.vnet[i]
        pad = r + m.clr + cap
        for j in m._cands(m.via_cells, (), vx, vy, vx, vy, pad):
            if j == i or m.vnet[j] == net:
                continue
            ox, oy = m.via[j]
            d = math.hypot(vx - ox, vy - oy)
            ov = (r + m.vsize[j] / 2 + m.clr) - d
            if 1e-6 < ov <= cap:
                u = _unit_away((ox, oy), (vx, vy))
                if u:
                    move_via(i, u[0], u[1], ov)
                    vx, vy = m.via[i]
        for j in m._all_layer_pad_cands(vx, vy, pad):
            p = m.pads[j]
            if p.net == net:
                continue
            d = p.dist_to_pt(vx, vy)
            ov = (r + m.clr) - d
            if 1e-6 < ov <= cap:
                u = _unit_away((p.cx, p.cy), (vx, vy))
                if u:
                    move_via(i, u[0], u[1], ov)
                    vx, vy = m.via[i]
    return accepted


def nudge_board(input_file, output_file, clearance, grid_step=0.1, max_passes=8,
                jog=False, verbose=True):
    pcb = parse_kicad_pcb(input_file)
    m = _Model(pcb, clearance, grid_step)
    # Jog (split a both-ends-anchored track and offset the middle) is OFF by
    # default: it clears a few extra sub-cell grazes but the inserted connectors
    # tend to introduce a similar number of larger violations -- net marginal.
    m.jog = jog
    total = 0
    for p in range(max_passes):
        n = _nudge_pass(m)
        total += n
        if verbose:
            print(f"  pass {p+1}: nudged {n} joint(s)")
        if n == 0:
            break
    changed_segs, killed_segs, added_segs = {}, set(), []
    for i in range(len(m.seg)):
        orig = m.seg_orig[i]
        if orig is None:                       # jog-inserted segment
            if m.alive[i]:
                sx, sy, ex, ey = m.seg[i]
                added_segs.append((sx, sy, ex, ey, m.sw[i], m.slayer[i],
                                   m.net_name.get(m.snet[i]), m.snet[i]))
        elif not m.alive[i]:                   # original replaced by a jog
            killed_segs.add(orig)
        elif tuple(m.seg[i]) != orig:          # original nudged in place
            changed_segs[orig] = tuple(m.seg[i])
    changed_vias = {m.via_orig[i]: tuple(m.via[i])
                    for i in range(len(m.via)) if tuple(m.via[i]) != m.via_orig[i]}
    _write_back(input_file, output_file, changed_segs, changed_vias, killed_segs, added_segs)
    if verbose:
        print(f"Nudged {total} joint(s); rewrote {len(changed_segs)} segment end(s), "
              f"{len(changed_vias)} via(s), {len(added_segs)} jog segment(s) "
              f"({len(killed_segs)} split). Wrote {output_file}")
    return total


def _fmt(v):
    return f"{v:.6f}"


def _write_back(input_file, output_file, changed_segs, changed_vias,
                killed_segs=frozenset(), added_segs=()):
    from kicad_writer import generate_segment_sexpr
    with open(input_file, 'r', encoding='utf-8') as f:
        text = f.read()
    num = r'-?\d+\.?\d*'

    def seg_repl(mo):
        block = mo.group(0)
        sm = re.search(rf'\(start\s+({num})\s+({num})\)', block)
        em = re.search(rf'\(end\s+({num})\s+({num})\)', block)
        if not sm or not em:
            return block
        key = (float(sm.group(1)), float(sm.group(2)), float(em.group(1)), float(em.group(2)))
        if key in killed_segs:                 # replaced by a jog -> drop the block
            return ''
        new = changed_segs.get(key)
        if not new:
            return block
        block = block[:sm.start()] + f'(start {_fmt(new[0])} {_fmt(new[1])})' + block[sm.end():]
        em = re.search(rf'\(end\s+({num})\s+({num})\)', block)
        return block[:em.start()] + f'(end {_fmt(new[2])} {_fmt(new[3])})' + block[em.end():]

    def via_repl(mo):
        block = mo.group(0)
        am = re.search(rf'\(at\s+({num})\s+({num})\)', block)
        if not am:
            return block
        new = changed_vias.get((float(am.group(1)), float(am.group(2))))
        if not new:
            return block
        return block[:am.start()] + f'(at {_fmt(new[0])} {_fmt(new[1])})' + block[am.end():]

    text = re.sub(r'\(segment\b(?:[^()]|\([^()]*\))*\)', seg_repl, text)
    text = re.sub(r'\(via\b(?:[^()]|\([^()]*\))*\)', via_repl, text)

    if added_segs:
        blocks = [generate_segment_sexpr((sx, sy), (ex, ey), w, layer, nid, net_name=name)
                  for (sx, sy, ex, ey, w, layer, name, nid) in added_segs]
        ins = '\n' + '\n'.join(blocks) + '\n'
        last = text.rstrip().rfind(')')
        text = text[:last] + ins + text[last:]

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(text)


def main():
    ap = argparse.ArgumentParser(description="Nudge sub-cell clearance grazes (issue #70).")
    ap.add_argument('input')
    ap.add_argument('output')
    ap.add_argument('--clearance', type=float, required=True, help='Design clearance floor (mm)')
    ap.add_argument('--grid-step', type=float, default=0.1, help='Routing grid step (mm); move cap = half this')
    ap.add_argument('--max-passes', type=int, default=8)
    ap.add_argument('--jog', action='store_true',
                    help='Also split both-ends-anchored tracks and offset the middle '
                         '(marginal; can add a few larger violations -- off by default)')
    args = ap.parse_args()
    nudge_board(args.input, args.output, args.clearance, args.grid_step, args.max_passes, args.jog)


if __name__ == '__main__':
    main()
