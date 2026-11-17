#!/bin/bash
# Build Todone.app (release) next to this script, ad-hoc signed for local use.
#
#   ./build-app.sh            # then: open Todone.app
#   open Todone.app --args --fake    # sample data, nothing written to Todoist
#
# The app runs the Python helper from this repo checkout, whose path is recorded
# in the bundle's Info.plist, so rebuild if you move the repo.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/.." && pwd)"
app="$here/Todone.app"

swift build -c release --package-path "$here"
bin="$(swift build -c release --package-path "$here" --show-bin-path)/Todone"

rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$bin" "$app/Contents/MacOS/Todone"

# App icon, drawn by make-icon.swift (a green check).
iconset="$(mktemp -d)/AppIcon.iconset"
swift "$here/make-icon.swift" "$iconset"
iconutil -c icns "$iconset" -o "$app/Contents/Resources/AppIcon.icns"
rm -rf "$(dirname "$iconset")"

cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Todone</string>
  <key>CFBundleDisplayName</key><string>Todone</string>
  <key>CFBundleIdentifier</key><string>com.rubick.todone</string>
  <key>CFBundleExecutable</key><string>Todone</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundleShortVersionString</key><string>0.1</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>TodoneRepoPath</key><string>$repo</string>
</dict>
</plist>
PLIST

# Ad-hoc signature: enough to run locally, no developer account needed.
codesign --force --sign - "$app"
echo "Built $app"
