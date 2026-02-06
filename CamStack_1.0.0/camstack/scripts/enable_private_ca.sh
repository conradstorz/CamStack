#!/usr/bin/env bash
set -euo pipefail
. /opt/camstack/scripts/pki.sh

CN="${1:-camstack.lan}"

echo "[*] Enabling Private CA mode for CN=$CN"
ensure_private_ca_local
sign_with_local_ca "$CN"
systemctl restart camstack.service
echo "[✓] Private CA mode active. Import CA on clients: /opt/camstack/ca/rootCA.crt"
