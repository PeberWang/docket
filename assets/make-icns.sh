#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
rm -rf Docket.iconset
mkdir Docket.iconset
for s in 16 32 64 128 256 512; do
  sips -z $s $s docket-icon-1024.png --out Docket.iconset/icon_${s}x${s}.png >/dev/null
  d=$((s*2))
  sips -z $d $d docket-icon-1024.png --out Docket.iconset/icon_${s}x${s}@2x.png >/dev/null
done
sips -z 1024 1024 docket-icon-1024.png --out Docket.iconset/icon_512x512@2x.png >/dev/null
iconutil -c icns Docket.iconset -o Docket.icns
ls -la Docket.icns
