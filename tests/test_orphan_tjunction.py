#!/usr/bin/env python3
"""check_orphan_stubs must not count connected T-junction tap landings (#84).

A degree-1 endpoint that lands on the interior copper of another same-net segment
is a T-junction -- the traces overlap, so it is electrically connected, not a
dead end. Only genuine dead ends (and near-misses that do not overlap) stay.

    python3 tests/test_orphan_tjunction.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from check_orphan_stubs import _on_segment_interior


def run():
    fails = []

    def check(name, cond):
        if not cond:
            fails.append(name)

    trunk = {'start': (0, 0), 'end': (10, 0), 'width': 0.2}
    tap = {'start': (5, 0), 'end': (5, 3), 'width': 0.13}

    # Tap end lands on trunk interior -> connected (excluded from orphans).
    check("T-junction landing is connected", _on_segment_interior((5, 0), [trunk, tap]) is True)
    # The tap's far end lands on nothing -> genuine orphan.
    check("dead tip stays an orphan", _on_segment_interior((5, 3), [trunk, tap]) is False)

    # Landing near a trunk endpoint (interior, not at the vertex) still connects.
    tap2 = {'start': (0.3, 0), 'end': (0.3, 2), 'width': 0.13}
    check("near-endpoint interior landing connects",
          _on_segment_interior((0.3, 0), [trunk, tap2]) is True)

    # A stub passing 0.3 mm clear of the trunk does NOT connect (no over-clearing).
    miss = {'start': (5, 0.3), 'end': (5, 3), 'width': 0.13}
    check("0.3mm near-miss is not cleared", _on_segment_interior((5, 0.3), [trunk, miss]) is False)

    # A landing exactly at a trunk vertex is handled by the degree count, not here
    # (t at an endpoint is excluded), so the interior test reports False there.
    end_pt = {'start': (10, 0), 'end': (10, 4), 'width': 0.13}
    check("landing at a vertex is not an interior hit",
          _on_segment_interior((10, 0), [trunk, end_pt]) is False)

    print("=" * 60)
    if fails:
        for f in fails:
            print(f"  FAIL  {f}")
        print(f"\n{len(fails)} failure(s)")
        return 1
    print("  PASS  T-junction landings excluded; dead ends, near-misses,")
    print("        and vertex landings handled correctly (5 cases)")
    print("\n5/5 checks passed")
    return 0


if __name__ == '__main__':
    sys.exit(run())
