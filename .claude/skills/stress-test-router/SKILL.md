---
name: stress-test-router
description: Stress-tests the router against real-world open-source KiCad boards (downloaded, normalized, stripped of routing), measuring routing completion rates and DRC violations per board. Aggregates results and files GitHub issues for new router/parser findings after user approval. Use to regression-test the router at scale or to hunt for robustness issues.
---

# Stress-Test Router on Real-World Boards

Run the `tests/stress/` harness end-to-end: prepare the board corpus, route
every board following the plan-pcb-routing skill workflow, aggregate
completion/DRC statistics, and turn novel findings into GitHub issues.

All corpus artifacts live OUTSIDE the repo in `$STRESS_DIR`
(default `~/Documents/kicad_stress_test`). Never commit boards to the repo.

## Step 1: Prepare the corpus (skip parts that already exist)

Check `$STRESS_DIR/boards_unrouted/*.kicad_pcb` first — if the corpus exists
and parses (run `tests/stress/validate_boards.py`), skip to Step 2.

```bash
cd tests/stress
python3 fetch_boards.py            # downloads from GitHub (needs `gh` auth)

KIPY=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
$KIPY normalize_boards.py          # pcbnew round-trip -> current format
for f in "$STRESS_DIR"/boards/*.kicad_pcb; do   # ONE board per process
  $KIPY strip_routing.py "$(basename "${f%.kicad_pcb}")"
done
python3 validate_boards.py         # all boards must parse with sane stats
```

Platform note: on Linux/Windows find the KiCad-bundled python equivalent, or
any python with a working `pcbnew` module of KiCad 9+.

To extend the corpus, add `(owner/repo, note)` entries to `REPOS` in
`fetch_boards.py` and a fragment->name mapping in `normalize_boards.py`.
Only KiCad 6+ sources survive; older ones are rescued by the pcbnew
round-trip.

## Step 2: Run boards (subagents, bounded)

For each board in the corpus without a fresh `$STRESS_DIR/results/<board>.json`,
spawn a general-purpose subagent with this prompt skeleton:

> Read $STRESS_DIR/RUNBOOK.md (fall back to tests/stress/RUNBOOK.md) and
> execute it for BOARD=<name> (<one-line complexity hint>). Follow the
> runbook exactly: analyze per the plan-pcb-routing skill, route with the
> repo's tools, verify, and write $STRESS_DIR/results/<name>.json. Never
> modify anything under the tools repo.

Hard operational limits (violating these has crashed the machine before):

- **Max 2 boards in flight at once.** Launch two, wait for a completion
  notification, backfill.
- **Every tool command inside a run goes through
  `tests/stress/run_limited.sh`** (~1 GB RSS watchdog). An OOM kill is a
  finding, not noise.
- Order boards simple -> complex (keyboards first, BGA/SoC boards last) so
  harness problems surface cheaply.
- Subagents must not end their turn while a routing process is still
  running (the run gets orphaned — runbook rule 11).

## Step 3: Aggregate

When all boards have results JSONs, build a summary table sorted by
completion rate: board, layers, routable nets, completion %, multipoint pads
connected/total, DRC baseline/final/delta, connectivity verdict, orphan
stubs, wall time, issue count. Flag:

- completion < 100% — which nets, what failure mode
- DRC delta > 0 — violation types introduced by the router
- crashes / hangs / OOM kills — always report, with tracebacks
- per-board `issues` lists — deduplicate into distinct findings

## Step 4: File GitHub issues (with approval)

For each distinct finding:

1. Search for an existing issue first:
   `gh issue list --search "<keywords>" --state all` — skip duplicates
   (comment on the existing issue instead if there's new evidence).
2. Draft: title, affected boards, reproduction command (exact tool
   invocation against the corpus board), observed vs expected, relevant log
   excerpt, and severity (router-correctness > parser-robustness >
   route-quality > workflow-friction).
3. **Present all drafts to the user and get approval BEFORE creating any
   issue.** File only approved ones with `gh issue create`.

Known findings already on record (do not re-file; they are documented in
`tests/stress/README.md`): power-type copper layers dropped by the parser;
Edge.Cuts regex cross-matching through other graphics; KiCad 6/7
fp_text-reference footprint collapse.

## Reporting

End with: the summary table, the list of new issues filed (numbers/links),
duplicates skipped, and any corpus-preparation problems. Keep per-board
detail in the results JSONs, not the chat.
