#!/usr/bin/env bash
set -euo pipefail

TLS_MODE="${CAMSTACK_TLS_MODE:-selfsigned}"  # selfsigned | private_ca_local | csr_only
CN_DEFAULT="${CAMSTACK_CN:-camstack.lan}"    # Common Name for cert

echo "[*] Preparing system packages..."
apt update
apt install -y mpv ffmpeg yt-dlp python3 python3-venv jq git tree curl openssl systemd

mkdir -p /opt/camstack/runtime/snaps /opt/camstack/logs /opt/camstack/certs /opt/camstack/ca
cd /opt/camstack

# Install uv if not present
if ! command -v uv >/dev/null 2>&1; then
  echo "[*] Installing uv (Astral)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

echo "[*] Creating venv & installing Python deps (via uv)..."
uv sync

# TLS setup
. /opt/camstack/scripts/pki.sh

case "$TLS_MODE" in
  selfsigned)
    echo "[*] TLS mode: self-signed"
    ensure_self_signed
    ;;
  private_ca_local)
    echo "[*] TLS mode: local Private CA (on this device)"
    ensure_private_ca_local
    sign_with_local_ca "$CN_DEFAULT"
    ;;
  csr_only)
    echo "[*] TLS mode: CSR-only (offline CA flow)"
    generate_csr_only "$CN_DEFAULT"
    ;;
  *)
    echo "[!] Unknown CAMSTACK_TLS_MODE=$TLS_MODE; defaulting to self-signed"
    ensure_self_signed
    ;;
esac

echo "[*] Installing systemd services..."
for svc in services/*.service; do
  ln -sf "/opt/camstack/$svc" "/etc/systemd/system/$(basename "$svc")"
done
systemctl daemon-reload
systemctl enable camredirect.service camstack.service camplayer.service
systemctl restart camredirect.service camstack.service camplayer.service

echo
echo "[✓] CamStack 1.0.0 is up."
echo "    HTTPS Admin UI: https://$(hostname -I | awk '{print $1}')/"
if [ "$TLS_MODE" = "selfsigned" ]; then
  echo "    (Self-signed cert — browser will warn until you trust it.)"
elif [ "$TLS_MODE" = "private_ca_local" ]; then
  echo "    Trust this CA on clients: /opt/camstack/ca/rootCA.crt"
elif [ "$TLS_MODE" = "csr_only" ]; then
  echo "    CSR ready at: /opt/camstack/certs/server.csr"
  echo "    After you sign it, save as /opt/camstack/certs/server.crt then:"
  echo "      sudo systemctl restart camstack.service"
fi
