#!/bin/bash
# Builds "Shoebox.app" and a .dmg. Run on a Mac (the GitHub workflow does this for you).
# Optional environment variables for signing + notarizing:
#   SIGN_ID        "Developer ID Application: Your Name (TEAMID)"
#   APPLE_ID  APPLE_APP_PASSWORD  APPLE_TEAM_ID     (for notarization)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"; WORK="$ROOT/build_mac"; ARCH="$(uname -m)"          # arm64 or x86_64
APPVER="${APP_VERSION:-1.0.0}"
rm -rf "$WORK" dist; mkdir -p "$WORK/bin" "$WORK/dl"

echo "== ExifTool"
TAG=$(git ls-remote --tags --refs https://github.com/exiftool/exiftool | sed 's#.*/##' | grep -E '^[0-9]+\.[0-9]+$' | sort -V | tail -1)
git clone --depth 1 --branch "$TAG" https://github.com/exiftool/exiftool "$WORK/exiftool_src"
mkdir -p "$WORK/exiftool"; cp -R "$WORK/exiftool_src/exiftool" "$WORK/exiftool_src/lib" "$WORK/exiftool/"
echo "ExifTool $TAG"

echo "== ffmpeg / ffprobe ($ARCH)"
case "$ARCH" in arm64) PAT="darwin-arm64";; *) PAT="darwin-x64";; esac
gh release download --repo eugeneware/ffmpeg-static --pattern "*${PAT}*" --dir "$WORK/dl" --clobber
ls -la "$WORK/dl"
for t in ffmpeg ffprobe; do
  f=$(ls "$WORK/dl" | grep -i "^${t}" | grep -v '\.LICENSE\|\.README' | head -1 || true)
  [ -n "$f" ] || { echo "Could not find $t in the download. Adjust packaging/build_mac.sh"; exit 1; }
  case "$f" in *.gz) gunzip -c "$WORK/dl/$f" > "$WORK/bin/$t";; *) cp "$WORK/dl/$f" "$WORK/bin/$t";; esac
  chmod +x "$WORK/bin/$t"
done
"$WORK/bin/ffmpeg" -version | head -1; "$WORK/bin/ffprobe" -version | head -1

echo "== Icon"
ISET="$WORK/icon.iconset"; mkdir -p "$ISET"
for sz in 16 32 64 128 256 512; do
  sips -z $sz $sz packaging/icon.png --out "$ISET/icon_${sz}x${sz}.png" >/dev/null
  sips -z $((sz*2)) $((sz*2)) packaging/icon.png --out "$ISET/icon_${sz}x${sz}@2x.png" >/dev/null
done
iconutil -c icns "$ISET" -o "$WORK/icon.icns"

echo "== PyInstaller"
python3 -m pip install --quiet pyinstaller pywebview
python3 -m PyInstaller --noconfirm --windowed --name "Shoebox" \
  --icon "$WORK/icon.icns" \
  --osx-bundle-identifier com.daviddef.metadatafixer \
  ${SIGN_ID:+--codesign-identity "$SIGN_ID" --osx-entitlements-file packaging/entitlements.plist} \
  --paths "$ROOT" \
  --add-binary "$WORK/bin/ffmpeg:bin" --add-binary "$WORK/bin/ffprobe:bin" \
  --add-data "$WORK/exiftool:exiftool" --add-data "$ROOT/places.csv.gz:." --add-data "$ROOT/photos_issues.json:." --add-data "$ROOT/USER_GUIDE.md:." --add-data "$ROOT/LICENSE:." --add-data "$ROOT/THIRD_PARTY_NOTICES.md:." \
  --hidden-import takeout_gui --hidden-import takeout_fix_metadata \
  --distpath dist --workpath "$WORK/pyi" --specpath "$WORK" \
  packaging/app_main.py
APP="dist/Shoebox.app"
/usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $APPVER" "$APP/Contents/Info.plist" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $APPVER" "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :NSDesktopFolderUsageDescription string Shoebox saves its reports on your Desktop." "$APP/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :NSRemovableVolumesUsageDescription string Shoebox reads and organises the photos on your drives." "$APP/Contents/Info.plist" 2>/dev/null || true

if [ -n "${SIGN_ID:-}" ]; then
  echo "== Sign (inside out)"
  find "$APP" -type f \( -perm -u+x -o -name '*.dylib' -o -name '*.so' \) -print0 | while IFS= read -r -d '' f; do
    file "$f" | grep -q 'Mach-O' && codesign --force --options runtime --timestamp --entitlements packaging/entitlements.plist -s "$SIGN_ID" "$f"
  done
  codesign --force --options runtime --timestamp --entitlements packaging/entitlements.plist -s "$SIGN_ID" "$APP"
  codesign --verify --deep --strict --verbose=2 "$APP"
fi

DMG="dist/Shoebox-${APPVER}-${ARCH}.dmg"
echo "== DMG"
STAGE="$WORK/dmg"; mkdir -p "$STAGE"; cp -R "$APP" "$STAGE/"; ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "Shoebox" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
[ -n "${SIGN_ID:-}" ] && codesign --force --timestamp -s "$SIGN_ID" "$DMG"

if [ -n "${SIGN_ID:-}" ] && [ -n "${APPLE_ID:-}" ]; then
  echo "== Notarize"
  xcrun notarytool submit "$DMG" --apple-id "$APPLE_ID" --password "$APPLE_APP_PASSWORD" --team-id "$APPLE_TEAM_ID" --wait
  xcrun stapler staple "$DMG"
  spctl -a -t open --context context:primary-signature -v "$DMG" || true
fi
echo "Built: $DMG"
