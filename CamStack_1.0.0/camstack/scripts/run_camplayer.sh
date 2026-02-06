#!/usr/bin/env bash
set -euo pipefail
cd /opt/camstack

# Generate overlay
/opt/camstack/.venv/bin/python -m app.overlay_gen

# Run player with watchdog support
/opt/camstack/.venv/bin/python - <<'PY'
from app.player import launch_rtsp_with_watchdog
import sys
sys.exit(launch_rtsp_with_watchdog())
PY
