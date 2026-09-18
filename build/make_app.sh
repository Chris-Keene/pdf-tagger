#!/bin/bash
# Build "PDF Tagger.app" — a self-contained bundle that needs no Python on the
# machine it runs on.  Run from the project root: ./build/make_app.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
VENV="$ROOT/.venv"

[ -x "$VENV/bin/python" ] || { echo "No venv. See README."; exit 1; }
"$VENV/bin/python" build/make_icon.py

# veraPDF + a trimmed JRE, so recipients get a real conformance verdict.
# --no-verapdf skips it and saves about 64MB.
WITH_VERAPDF=1
for arg in "$@"; do [ "$arg" = "--no-verapdf" ] && WITH_VERAPDF=0; done
if [ "$WITH_VERAPDF" = "1" ]; then
  if [ ! -d "$ROOT/build/verapdf" ]; then ./build/bundle_verapdf.sh; fi
else
  echo "skipping veraPDF (--no-verapdf)"
fi

rm -rf build/dist build/work
"$VENV/bin/pyinstaller" \
  --noconfirm --clean --windowed \
  --name "PDF Tagger" \
  --icon "$ROOT/build/icon/PDFTagger.icns" \
  --add-data "$ROOT/app/ui:app/ui" \
  --hidden-import objc --hidden-import AppKit --hidden-import Foundation \
  --hidden-import WebKit --hidden-import CoreFoundation \
  --osx-bundle-identifier "me.chriskeene.pdftagger" \
  --distpath build/dist --workpath build/work --specpath build \
  run.py

APP="build/dist/PDF Tagger.app"

# Copied in with ditto rather than --add-data: the JRE has symlinks and executable
# bits that PyInstaller's data copying does not preserve.
if [ "$WITH_VERAPDF" = "1" ] && [ -d "$ROOT/build/verapdf" ]; then
  ditto "$ROOT/build/verapdf" "$APP/Contents/Resources/verapdf"
  echo "  bundled veraPDF into Contents/Resources/verapdf"
fi
# Deliberately NOT LSUIElement: the UI lives in a browser tab, so without a Dock
# icon a user who closes the tab has no way to stop the server short of Activity
# Monitor.  A Dock icon gives them right-click - Quit as a reliable escape hatch.
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString 1.20.0" "$APP/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleVersion string 1.20.0" "$APP/Contents/Info.plist" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Set :CFBundleVersion 1.20.0" "$APP/Contents/Info.plist" 2>/dev/null || true

# Let a PDF be opened with this app (right-click - Open With, or dropped on the
# Dock icon). Rank Alternate on purpose: hijacking the default PDF handler would
# be a rude thing for a tagging tool to do.
PL="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Delete :CFBundleDocumentTypes" "$PL" 2>/dev/null || true
/usr/libexec/PlistBuddy \
  -c "Add :CFBundleDocumentTypes array" \
  -c "Add :CFBundleDocumentTypes:0 dict" \
  -c "Add :CFBundleDocumentTypes:0:CFBundleTypeName string PDF document" \
  -c "Add :CFBundleDocumentTypes:0:CFBundleTypeRole string Editor" \
  -c "Add :CFBundleDocumentTypes:0:LSHandlerRank string Alternate" \
  -c "Add :CFBundleDocumentTypes:0:LSItemContentTypes array" \
  -c "Add :CFBundleDocumentTypes:0:LSItemContentTypes:0 string com.adobe.pdf" \
  "$PL" >/dev/null 2>&1 || true
# A window-owning app should not claim to be a background agent.
/usr/libexec/PlistBuddy -c "Delete :LSUIElement" "$PL" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :NSHighResolutionCapable bool true" "$PL" 2>/dev/null || true
# Ad-hoc sign LAST: the signature seals the resource list, so anything copied in
# afterwards invalidates it.
codesign --force --deep --sign - "$APP" 2>/dev/null || echo "note: ad-hoc signing skipped"

# PyInstaller leaves two 92MB duplicates behind: a loose onedir build beside the
# bundle, and a second .app next to the spec file. The one in dist/ is complete
# and self-contained, so both copies are pure waste.
# PyInstaller scatters duplicates: a loose onedir build beside the bundle, a second
# .app next to the spec, and a third in the project's own dist/ where the shippable
# zip lives. Three copies of 160MB, and the one in dist/ is the dangerous one because
# someone could send it instead of the zip.
rm -rf "build/dist/PDF Tagger" "build/PDF Tagger.app" "$ROOT/dist/PDF Tagger.app"

echo
echo "Built: $ROOT/$APP"
du -sh "$APP"
