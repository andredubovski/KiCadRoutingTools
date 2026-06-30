#!/bin/bash
# Normalize+strip every set-4 board (one pcbnew process each, segfault-safe).
# Reuses prep_set2.py (generic: <src> <routed_dst> <stripped_dst>).
#   stripped, unrouted boards -> boards_unrouted_set4/
#   normalized routed reference + .kicad_pro -> boards_set4/
# Set-4 sources are current KiCad (v10), so LoadBoard reads them directly.
set -u
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STRESS="${STRESS_DIR:-$HOME/Documents/kicad_stress_test}"
SRC="$STRESS/sources/github_set4"
KPY="${KICAD_PYTHON:-/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3}"
PREP="$SELF/prep_set2.py"
mkdir -p "$STRESS/boards_unrouted_set4" "$STRESS/boards_set4"

# short-name | source-filename-fragment (unique within github_set4/)
MAP=(
  "fpga_sdram|Rahul9-spb__FPGA-1__"
)

for entry in "${MAP[@]}"; do
  IFS='|' read -r name frag <<< "$entry"
  srcfile=$(find "$SRC" -maxdepth 1 -name '*.kicad_pcb' -name "*${frag}*" | head -1)
  if [ -z "$srcfile" ]; then echo "MISS $name (frag '$frag')"; continue; fi
  echo "== $name <- $(basename "$srcfile")"
  "$KPY" "$PREP" "$srcfile" "$STRESS/boards_set4/$name.kicad_pcb" \
         "$STRESS/boards_unrouted_set4/$name.kicad_pcb" 2>/dev/null || echo "  FAIL $name"
  profile="${srcfile%.kicad_pcb}.kicad_pro"
  [ -f "$profile" ] && cp "$profile" "$STRESS/boards_set4/$name.kicad_pro"
done
echo "Done. stripped -> boards_unrouted_set4/ ; routed reference -> boards_set4/"
