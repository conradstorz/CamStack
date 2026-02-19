#!/usr/bin/env bash
#
# CamStack 2.0.1 Development-Mode Installer
#
# This installer creates SYMLINKS instead of copying files, enabling live development:
#   - /opt/camstack -> symlink to source directory (this repository)
#   - systemd service files -> symlinked to source
#   - Runtime files (logs, certs, configs) -> stored in source tree
#
# BENEFITS:
#   • Edit code in this repository and see changes immediately
#   • Single source of truth - no file duplication
#   • All changes can be committed to version control
#   • Easy rollback via git checkout
#
# REQUIREMENT: Do not move or delete this directory after installation!
#              The system will reference files from this location.
#

set -euo pipefail  # Exit on error, undefined variables, or pipe failures

# ==============================================================================
# CONFIGURATION
# ==============================================================================

# Determine the absolute path to this script's directory
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source directory containing all CamStack files
SRC="$HERE/camstack"

# System-level installation directory (will be a symlink)
DST="/opt/camstack"

# ==============================================================================
# INSTALLATION
# ==============================================================================

echo ""
echo "╔═══════════════════════════════════════════════════════════════════════╗"
echo "║              CamStack 2.0.1 Development Mode Installer                ║"
echo "╚═══════════════════════════════════════════════════════════════════════╝"
echo ""
echo "Installation will create symlinks (not copy files) to enable live development."
echo ""
echo "  Repository location: $SRC"
echo "  Symlink target:      $DST"
echo ""

# ------------------------------------------------------------------------------
# Step 1: Handle existing installation (if any)
# ------------------------------------------------------------------------------

if [ -e "$DST" ]; then
  if [ -L "$DST" ]; then
    # Existing installation is already a symlink
    echo "[*] Found existing symlink at $DST"
    echo "    Removing old symlink to replace with new one..."
    sudo rm "$DST"
  else
    # Existing installation is a regular directory (old file-based install)
    echo "[*] Found existing file-based installation at $DST"
    echo "    This appears to be an old installation using copied files."
    echo "    Backing up to ${DST}.backup..."
    echo ""
    echo "    NOTE: You can safely delete ${DST}.backup after verifying"
    echo "          the new installation works correctly."
    sudo mv "$DST" "${DST}.backup"
  fi
  echo ""
fi

# ------------------------------------------------------------------------------
# Step 2: Create symlink from /opt/camstack to this repository
# ------------------------------------------------------------------------------

echo "[*] Creating symlink: $DST -> $SRC"
sudo ln -s "$SRC" "$DST"
echo "    ✓ Symlink created successfully"
echo ""

# ------------------------------------------------------------------------------
# Step 3: Run the main installation script
# ------------------------------------------------------------------------------
# This will:
#   - Install system dependencies (mpv, ffmpeg, python, etc.)
#   - Set up Python virtual environment
#   - Install TLS certificates
#   - Install and enable systemd services (as symlinks)
#   - Start services

echo "[*] Running system installation script..."
echo "    This will install dependencies and configure services."
echo ""
cd "$SRC/scripts"
bash install_camstack.sh

# ------------------------------------------------------------------------------
# Installation Complete
# ------------------------------------------------------------------------------

echo ""
echo "╔═══════════════════════════════════════════════════════════════════════╗"
echo "║                     Installation Complete!                           ║"
echo "╚═══════════════════════════════════════════════════════════════════════╝"
echo ""
echo "CamStack is now running in DEVELOPMENT MODE."
echo ""
echo "What this means:"
echo "  • All files remain in: $SRC"
echo "  • Edit code there and changes take effect immediately"
echo "  • After editing, restart the relevant service:"
echo "      sudo systemctl restart camstack.service"
echo "      sudo systemctl restart camplayer.service"
echo ""
echo "See DEVELOPMENT_GUIDE.md for detailed development workflow."
echo ""
