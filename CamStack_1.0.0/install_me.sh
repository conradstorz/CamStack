#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/camstack"
DST="/opt/camstack"
echo "[*] Copying files to $DST ..."
mkdir -p "$DST"
cp -a "$SRC/." "$DST/"
echo "[*] Running installer ..."
cd "$DST/scripts"
bash install_camstack.sh
echo "[✓] CamStack 1.0.0 install complete."
