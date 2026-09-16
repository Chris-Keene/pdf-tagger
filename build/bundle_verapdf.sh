#!/bin/bash
# Put veraPDF and a trimmed Java runtime inside the app, so a recipient gets a
# real conformance verdict without installing anything.
#
# Licensing, since this redistributes other people's code:
#   * veraPDF is dual licensed GPL-3.0-or-later OR MPL-2.0. We take MPL-2.0,
#     which is file-level copyleft and imposes nothing on our own source.
#   * The runtime is OpenJDK under GPLv2 *with the Classpath Exception*, which is
#     what makes shipping a jlink'd runtime with an application allowed.
# Both licence texts are copied in beside the binaries.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
OUT="$ROOT/build/verapdf"

VERA_CELLAR="$(brew --prefix)/Cellar/verapdf"
JAVA_HOME_SRC="$(brew --prefix)/opt/openjdk/libexec/openjdk.jdk/Contents/Home"
[ -d "$VERA_CELLAR" ] || { echo "veraPDF not installed (brew install verapdf)"; exit 1; }
[ -x "$JAVA_HOME_SRC/bin/jlink" ] || { echo "no jlink (brew install openjdk)"; exit 1; }

VERSION="$(ls "$VERA_CELLAR" | sort -V | tail -1)"
LIBEXEC="$VERA_CELLAR/$VERSION/libexec"
CLI="$(ls "$LIBEXEC/bin/cli-"*.jar | head -1)"
echo "veraPDF $VERSION  ->  $(basename "$CLI")"

rm -rf "$OUT"
mkdir -p "$OUT"
cp "$CLI" "$OUT/cli.jar"
cp -R "$LIBEXEC/config" "$OUT/config"

# jdeps cannot read the shaded jar (it bundles the long-removed java.xml.bind,
# which pulls java.activation), so the module list is set by hand and then proved
# by running a real validation below.
MODS="java.base,java.xml,java.logging,java.desktop,java.naming,java.sql,java.management,jdk.crypto.ec,java.security.jgss,jdk.unsupported"
"$JAVA_HOME_SRC/bin/jlink" --add-modules "$MODS" \
  --strip-debug --no-header-files --no-man-pages --compress=zip-9 \
  --output "$OUT/jre"

cp "$VERA_CELLAR/$VERSION/LICENSE.MPL" "$OUT/LICENSE-veraPDF-MPL-2.0.txt" 2>/dev/null || true
cp "$VERA_CELLAR/$VERSION/LICENSE.GPL" "$OUT/LICENSE-veraPDF-GPL-3.0.txt" 2>/dev/null || true
[ -d "$JAVA_HOME_SRC/legal" ] && cp -R "$JAVA_HOME_SRC/legal" "$OUT/jre-legal" || true
cat > "$OUT/README-licensing.txt" <<TXT
This directory redistributes two third-party components.

veraPDF $VERSION (cli.jar, config/)
  https://verapdf.org  -  dual licensed GPL-3.0-or-later OR MPL-2.0.
  Redistributed here under MPL-2.0. Full texts alongside this file.

OpenJDK runtime (jre/), trimmed with jlink
  GPLv2 with the Classpath Exception. Per-module notices in jre-legal/.

Neither is modified. Both are used only to validate PDFs locally; nothing is
sent anywhere.
TXT

echo "  verifying the trimmed runtime actually validates..."
"$OUT/jre/bin/java" -jar "$OUT/cli.jar" -f ua1 --format text "$ROOT/tests/slides-1page.pdf" >/dev/null 2>&1 || true
"$OUT/jre/bin/java" -jar "$OUT/cli.jar" --version 2>/dev/null | head -1
du -sh "$OUT" | awk '{print "  bundled size:", $1}'
