#!/bin/bash
# Export the install guide to an accessible PDF and prove it.
# Chrome's export is 102/106; harden_pdf.py and finish_pdf.py close the rest.
set -euo pipefail
cd "$(dirname "$0")/.."
SRC="${1:?usage: export_guide.sh <guide.html>}"
OUT="dist/Installing-Trellis.pdf"
# harden_pdf.py and verify_pdf_a11y.py come from the separate deck-a11y toolkit.
# Point DECK_A11Y at your checkout of it; without it there is nothing to export with.
DECK="${DECK_A11Y:-$HOME/Documents/Sandbox/deck-a11y/scripts}"
[ -d "$DECK" ] || { echo "set DECK_A11Y to your deck-a11y scripts dir" >&2; exit 1; }
mkdir -p dist
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless=new --disable-gpu --no-pdf-header-footer --virtual-time-budget=8000 \
  --print-to-pdf="/tmp/guide-raw.pdf" "file://$SRC" 2>/dev/null || true
python3 "$DECK/harden_pdf.py" /tmp/guide-raw.pdf -o /tmp/guide-hard.pdf | tail -3
./.venv/bin/python build/finish_pdf.py /tmp/guide-hard.pdf -o "$OUT"
rm -f /tmp/guide-raw.pdf /tmp/guide-hard.pdf
echo
verapdf -f ua1 --format text "$OUT" 2>/dev/null | head -1 || true
python3 "$DECK/verify_pdf_a11y.py" "$OUT" 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | tail -3
