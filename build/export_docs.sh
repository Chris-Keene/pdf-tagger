#!/bin/bash
# Build both shipped guides from docs/*.html into dist/*.pdf, and verify them.
#
# Chrome's own export reaches about 102 of 106; harden_pdf.py and finish_pdf.py close
# the rest. Both PDFs are then checked twice: veraPDF for PDF/UA-1 conformance, and
# deck-a11y's practical checker for the things conformance has no rule about.
set -euo pipefail
cd "$(dirname "$0")/.."
# harden_pdf.py and verify_pdf_a11y.py come from the separate deck-a11y toolkit.
# Point DECK_A11Y at your checkout of it; without it there is nothing to export with.
DECK="${DECK_A11Y:-$HOME/Documents/Sandbox/deck-a11y/scripts}"
[ -d "$DECK" ] || { echo "set DECK_A11Y to your deck-a11y scripts dir" >&2; exit 1; }
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
mkdir -p dist

build_one () {
  local src="$1" out="$2" label="$3"
  echo "=== $label ==="
  "$CHROME" --headless=new --disable-gpu --no-pdf-header-footer \
    --virtual-time-budget=12000 --print-to-pdf="/tmp/doc-raw.pdf" \
    "file://$PWD/$src" 2>/dev/null || true
  python3 "$DECK/harden_pdf.py" /tmp/doc-raw.pdf -o /tmp/doc-hard.pdf >/dev/null
  ./.venv/bin/python build/finish_pdf.py /tmp/doc-hard.pdf -o "$out" | tail -1
  rm -f /tmp/doc-raw.pdf /tmp/doc-hard.pdf
  verapdf -f ua1 --format text "$out" 2>/dev/null | head -1 || true
  python3 "$DECK/verify_pdf_a11y.py" "$out" 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | tail -2
  echo
}

./.venv/bin/python docs/make_docs.py
build_one docs/installing-trellis.html dist/Installing-Trellis.pdf "Install guide"
build_one docs/how-trellis-works.html     dist/How-Trellis-Works.pdf     "Engineering handover"
