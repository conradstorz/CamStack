#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/camstack"
DST="/opt/camstack"

echo "[*] Setting up $DST as symlink to development directory ..."

# Remove old installation if it exists
if [ -e "$DST" ]; then
  if [ -L "$DST" ]; then
    echo "    Removing existing symlink..."
    sudo rm "$DST"
  else
    echo "    Backing up existing installation to ${DST}.backup..."
    sudo mv "$DST" "${DST}.backup"
  fi
fi

# Create symlink to source directory for live development
echo "    Creating symlink: $DST -> $SRC"
sudo ln -s "$SRC" "$DST"

echo "[*] Running installer ..."
cd "$SRC/scripts"
bash install_camstack.sh
echo "[✓] CamStack 1.0.0 install complete (development mode - changes take effect immediately)."
