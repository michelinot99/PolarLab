#!/bin/bash
# Costruisce PolarLab.app: un lanciatore che esegue il codice di questa cartella con venv.nosync.
#
#   scripts/build_mac_app.sh              # installa in /Applications (o ~/Applications)
#   scripts/build_mac_app.sh ~/Desktop    # oppure nella cartella indicata
#
# Va rieseguito solo se sposti la cartella del progetto o cambia la versione di Python
# (3.13 → 3.14). Le modifiche al codice Python non richiedono di ricostruire l'app.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$DIR/venv.nosync/bin/python3"
if [ ! -x "$PY" ]; then
    echo "Manca l'ambiente virtuale. Crealo con:" >&2
    echo "  python3.13 -m venv venv.nosync && venv.nosync/bin/pip install -r requirements.txt" >&2
    exit 1
fi

# python3.x-config dell'interprete del venv (per Homebrew: /opt/homebrew/opt/python@3.x/bin)
PYVER="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
PYBIN="$("$PY" -c 'import sys; print(sys.base_prefix)')/bin"
CONFIG="$PYBIN/python$PYVER-config"
[ -x "$CONFIG" ] || CONFIG="$(command -v "python$PYVER-config" || true)"
if [ -z "$CONFIG" ] || [ ! -x "$CONFIG" ]; then
    echo "python$PYVER-config non trovato: serve un Python con gli header (Homebrew o python.org)." >&2
    exit 1
fi
# Percorsi /opt/homebrew/opt/...: restano validi dopo gli aggiornamenti di patch di Python
OPT="/opt/homebrew/opt/python@$PYVER"
CFLAGS="$("$CONFIG" --embed --cflags)"
LDFLAGS="$("$CONFIG" --embed --ldflags)"
if [ -d "$OPT" ]; then
    CELLAR="$("$PY" -c 'import sys; print(sys.base_prefix)')"
    CFLAGS="${CFLAGS//$CELLAR/$OPT/Frameworks/Python.framework/Versions/$PYVER}"
    LDFLAGS="${LDFLAGS//$CELLAR/$OPT/Frameworks/Python.framework/Versions/$PYVER}"
fi

DEST="${1:-/Applications}"
if [ -z "${1:-}" ] && [ ! -w "$DEST" ]; then
    DEST="$HOME/Applications"
fi
mkdir -p "$DEST"
APP="$DEST/PolarLab.app"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

echo "→ compilo il lanciatore (Python $PYVER)"
mkdir -p "$BUILD/PolarLab.app/Contents/MacOS" "$BUILD/PolarLab.app/Contents/Resources"
# shellcheck disable=SC2086
clang -O2 -Wall -Wno-unused-result -o "$BUILD/PolarLab.app/Contents/MacOS/PolarLab" \
    "$DIR/macos/launcher.c" $CFLAGS $LDFLAGS -framework CoreFoundation

echo "→ icona"
ICON="$DIR/ios/App/Resources/Assets.xcassets/AppIcon.appiconset/icon-1024.png"
SET="$BUILD/PolarLab.iconset"
mkdir -p "$SET"
for s in 16 32 128 256 512; do
    sips -z $s $s "$ICON" --out "$SET/icon_${s}x${s}.png" >/dev/null
    sips -z $((s * 2)) $((s * 2)) "$ICON" --out "$SET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$SET" -o "$BUILD/PolarLab.app/Contents/Resources/PolarLab.icns"

echo "→ Info.plist"
VERSION="$("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import polarlab; print(polarlab.__version__)' "$DIR")"
ESCAPED_DIR="$(printf '%s' "$DIR" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')"
sed -e "s|@PROJECT_DIR@|$ESCAPED_DIR|" -e "s|@VERSION@|$VERSION|g" "$DIR/macos/Info.plist.in" \
    > "$BUILD/PolarLab.app/Contents/Info.plist"
plutil -lint "$BUILD/PolarLab.app/Contents/Info.plist" >/dev/null
printf 'APPL????' > "$BUILD/PolarLab.app/Contents/PkgInfo"

echo "→ firma ad hoc"
codesign --force --sign - "$BUILD/PolarLab.app"

rm -rf "$APP"
mv "$BUILD/PolarLab.app" "$APP"
# Fa rileggere a Finder e Dock icona e tipi di documento
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" \
    >/dev/null 2>&1 || true
echo "✓ $APP"
echo "  Avvio: open \"$APP\"   ·   log: ~/Library/Logs/PolarLab/polarlab.log"
