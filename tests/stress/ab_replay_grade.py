#!/usr/bin/env python3
"""Replay a whole stress-test set into a fresh wave dir and grade it -- the A/B
harness on top of redo_stress_test.py (single-manifest replay).

`redo_stress_test.py` replays ONE board's recorded command manifest. This driver
replays every board in a set in parallel, then grades each board's *final* board
for DRC (at the route step's actual --clearance) and connectivity, and writes a
JSON summary. Run it once per code version ("wave") to A/B an engine change.

Two modes:

  # Grade one wave (the working tree decides which code runs):
  ab_replay_grade.py --set ~/Documents/kicad_stress_test/runs_set3 \
                     --out ~/Documents/kicad_stress_test/ab_run/old --label old

  # Compare two wave summaries:
  ab_replay_grade.py --compare .../old/summary.json .../new/summary.json

Typical A/B recipe (the engine change is uncommitted in the working tree):

  git stash push file1.py file2.py            # baseline = HEAD
  ab_replay_grade.py --set runs_set3 --out ab/old --label old
  git stash pop                               # candidate = HEAD + change
  ab_replay_grade.py --set runs_set3 --out ab/new --label new
  ab_replay_grade.py --compare ab/old/summary.json ab/new/summary.json

Notes / gotchas (see memory: rerun-stress-boards, grade-drc-at-routed-clearance):
- Manifests reference tools by ABSOLUTE repo path, so a replay always runs
  whatever is checked out -- the two waves MUST be sequential (shared git state),
  but boards WITHIN a wave run in parallel.
- A board whose chain breaks (e.g. a diff pair fails -> route_diff writes no
  output) reports chain_complete=False and is excluded from the DRC/conn
  comparison. Compare only counts boards complete in BOTH waves.
- DRC is graded at each board's own routed --clearance, parsed from its manifest.
"""
import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent  # tests/stress/ -> repo root


def route_clearance(manifest_txt, default="0.1"):
    """DRC must be graded at the routed clearance. A board may route different
    steps at different --clearance (e.g. a signal retry at 0.15 over a 0.1 base,
    plus planes at 0.1); grade at the MINIMUM so copper laid at the tightest
    clearance isn't phantom-flagged at a looser one (see grade-drc-at-routed-
    clearance: grading tigard at 0.15 vs its real 0.1 invents ~600 violations)."""
    vals = [float(v) for v in re.findall(r"--clearance\s+(\d[\d.]*)", manifest_txt)]
    return str(min(vals)) if vals else default


def final_output_name(manifest_txt):
    """Final board = last .kicad_pcb token of the last non-check command.

    Output naming varies per board (<board>_stepN, <board>_signal, step1_signal,
    ...), so detect it from the manifest rather than globbing a fixed pattern.
    For route*.py the output is the 2nd positional and for fanout it is --output;
    in both, the LAST .kicad_pcb token on the line is the produced board.
    """
    last = None
    for line in manifest_txt.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "check_" in line:
            continue
        toks = [t.strip("'\"") for t in line.split() if t.strip("'\"").endswith(".kicad_pcb")]
        if toks:
            last = toks[-1]
    return os.path.basename(last) if last else None


def _drc_count(text):
    m = re.search(r"FAILED \((\d+) violations?\)", text) or re.search(r"FOUND (\d+) DRC VIOLATION", text)
    return int(m.group(1)) if m else 0  # no match == PASSED / clean


def _conn_count(text):
    m = re.search(r"Connectivity issues \((\d+)\)", text)
    return int(m.group(1)) if m else 0


def grade(pcb, clearance):
    drc = subprocess.run([sys.executable, "-X", "utf8", str(REPO / "check_drc.py"), pcb,
                          "-c", clearance, "--quiet"], capture_output=True, text=True)
    conn = subprocess.run([sys.executable, "-X", "utf8", str(REPO / "check_connected.py"), pcb,
                           "--quiet"], capture_output=True, text=True)
    return _drc_count(drc.stdout + drc.stderr), _conn_count(conn.stdout + conn.stderr)


def do_board(set_dir, out_dir, label, board):
    manifest = set_dir / board / "redo_commands.sh"
    src = str(set_dir / board)
    dst = str(out_dir / board)
    Path(dst).mkdir(parents=True, exist_ok=True)
    txt = manifest.read_text()
    clr = route_clearance(txt)
    with open(f"{dst}/_replay.log", "w") as log:
        rc = subprocess.run([sys.executable, str(REPO / "tests/stress/redo_stress_test.py"),
                             str(manifest), "--remap", f"{src}:{dst}",
                             "--skip-checks", "--continue-on-error"],
                            stdout=log, stderr=subprocess.STDOUT).returncode
    fname = final_output_name(txt)
    final = os.path.join(dst, fname) if fname else None
    done = bool(final) and os.path.exists(final)
    res = {"board": board, "clearance": clr, "replay_rc": rc,
           "final": fname if done else None, "chain_complete": done,
           "drc": None, "conn": None}
    if done:
        res["drc"], res["conn"] = grade(final, clr)
    print(f"[{label}] {board}: chain={'ok' if done else 'BROKEN'} "
          f"drc={res['drc']} conn={res['conn']} final={res['final']}", flush=True)
    return res


def run_wave(set_dir, out_dir, label, jobs):
    boards = sorted(d.name for d in set_dir.iterdir() if (d / "redo_commands.sh").exists())
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[{label}] replaying {len(boards)} boards from {set_dir} -> {out_dir} ({jobs} parallel)")
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = [ex.submit(do_board, set_dir, out_dir, label, b) for b in boards]
        for f in concurrent.futures.as_completed(futs):  # report as boards finish
            results.append(f.result())
    results.sort(key=lambda r: r["board"])
    summary = out_dir / "summary.json"
    summary.write_text(json.dumps(results, indent=2))
    complete = sum(1 for r in results if r["chain_complete"])
    print(f"[{label}] wrote {summary}: {complete}/{len(results)} chains complete")
    return results


def regrade(out_dir, set_dir):
    """Re-grade an existing wave's final boards (no re-routing) and rewrite its
    summary.json -- e.g. after a grading fix like the route_clearance change, or
    to reuse a prior wave as a baseline."""
    out_dir = Path(out_dir); set_dir = Path(set_dir)
    results = []
    for bdir in sorted(p for p in out_dir.iterdir() if p.is_dir()):
        b = bdir.name
        man = set_dir / b / "redo_commands.sh"
        if not man.exists():
            continue
        txt = man.read_text(); clr = route_clearance(txt)
        fname = final_output_name(txt)
        final = bdir / fname if fname else None
        done = bool(final) and final.exists()
        res = {"board": b, "clearance": clr, "replay_rc": 0,
               "final": fname if done else None, "chain_complete": done,
               "drc": None, "conn": None}
        if done:
            res["drc"], res["conn"] = grade(str(final), clr)
        print(f"[regrade] {b}: chain={'ok' if done else 'BROKEN'} drc={res['drc']} conn={res['conn']}")
        results.append(res)
    (out_dir / "summary.json").write_text(json.dumps(results, indent=2))
    print(f"[regrade] rewrote {out_dir/'summary.json'}")
    return results


def compare(old_json, new_json):
    old = {r["board"]: r for r in json.loads(Path(old_json).read_text())}
    new = {r["board"]: r for r in json.loads(Path(new_json).read_text())}
    boards = sorted(set(old) | set(new))
    print(f"{'board':16} {'drc old->new':>14}  {'conn old->new':>15}  note")
    print("-" * 70)
    drc_delta = conn_delta = 0
    for b in boards:
        o, n = old.get(b), new.get(b)
        oc = o and o["chain_complete"]
        nc = n and n["chain_complete"]
        if not (oc and nc):
            print(f"{b:16} {'-':>14}  {'-':>15}  chain incomplete "
                  f"(old={'ok' if oc else 'broken'}, new={'ok' if nc else 'broken'}) -- excluded")
            continue
        dd = n["drc"] - o["drc"]
        cd = n["conn"] - o["conn"]
        drc_delta += dd
        conn_delta += cd
        flag = ""
        if dd > 0 or cd > 0:
            flag = "  <-- REGRESSION"
        elif dd < 0 or cd < 0:
            flag = "  improved"
        print(f"{b:16} {o['drc']:>5} -> {n['drc']:<5}  {o['conn']:>6} -> {n['conn']:<6} {flag}")
    print("-" * 70)
    verdict = "REGRESSION" if (drc_delta > 0 or conn_delta > 0) else "no regression"
    print(f"net delta: drc {drc_delta:+d}, conn {conn_delta:+d}  ==>  {verdict}")
    return drc_delta <= 0 and conn_delta <= 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", help="Set runs dir, e.g. ~/Documents/kicad_stress_test/runs_set3")
    ap.add_argument("--out", help="Wave output dir (per code version)")
    ap.add_argument("--label", default="wave", help="Label for log lines (e.g. old / new)")
    ap.add_argument("--jobs", type=int, default=4, help="Boards in parallel (default 4)")
    ap.add_argument("--compare", nargs=2, metavar=("OLD.json", "NEW.json"),
                    help="Compare two wave summaries and print a regression table")
    ap.add_argument("--regrade", metavar="WAVE_DIR",
                    help="Re-grade an existing wave's finals (no re-routing) and rewrite its summary.json")
    args = ap.parse_args()

    if args.compare:
        ok = compare(*args.compare)
        return 0 if ok else 1
    if args.regrade:
        if not args.set:
            ap.error("--regrade needs --set (for per-board manifests)")
        regrade(args.regrade, Path(args.set).expanduser())
        return 0
    if not args.set or not args.out:
        ap.error("--set and --out are required (or use --compare/--regrade)")
    run_wave(Path(args.set).expanduser(), Path(args.out).expanduser(), args.label, args.jobs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
