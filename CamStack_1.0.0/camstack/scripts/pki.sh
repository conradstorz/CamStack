#!/usr/bin/env bash
set -euo pipefail
CERT_DIR="/opt/camstack/certs"
CA_DIR="/opt/camstack/ca"
mkdir -p "$CERT_DIR" "$CA_DIR"

ensure_self_signed() {
  if [ -f "$CERT_DIR/server.crt" ] && [ -f "$CERT_DIR/server.key" ]; then
    echo "[*] Self-signed cert already present."
    return 0
  fi
  echo "[*] Generating self-signed server certificate..."
  openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
    -keyout "$CERT_DIR/server.key" \
    -out "$CERT_DIR/server.crt" \
    -subj "/CN=$(hostname -I | awk '{print $1}')/O=CamStack v1.0.0"
  chmod 600 "$CERT_DIR/server.key"
}

ensure_private_ca_local() {
  if [ -f "$CA_DIR/rootCA.crt" ] && [ -f "$CA_DIR/rootCA.key" ]; then
    echo "[*] Local CA already present."
    return 0
  fi
  echo "[*] Creating local CamStack Root CA (stored on this device)."
  openssl genrsa -out "$CA_DIR/rootCA.key" 4096
  openssl req -x509 -new -nodes -key "$CA_DIR/rootCA.key" -sha256 -days 3650 \
    -out "$CA_DIR/rootCA.crt" -subj "/C=US/O=CamStackLAN/CN=CamStack Root CA v1.0.0"
  chmod 600 "$CA_DIR/rootCA.key"
}

sign_with_local_ca() {
  local CN="$1"
  echo "[*] Issuing server certificate for CN=$CN using local CA..."
  openssl genrsa -out "$CERT_DIR/server.key" 2048
  openssl req -new -key "$CERT_DIR/server.key" -out "$CERT_DIR/server.csr" -subj "/CN=$CN"
  cat > "$CERT_DIR/v3.ext" <<EOEXT
basicConstraints=CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names
[alt_names]
DNS.1 = $CN
IP.1 = $(hostname -I | awk '{print $1}')
EOEXT
  openssl x509 -req -in "$CERT_DIR/server.csr" -CA "$CA_DIR/rootCA.crt" -CAkey "$CA_DIR/rootCA.key" \
    -CAcreateserial -out "$CERT_DIR/server.crt" -days 825 -sha256 -extfile "$CERT_DIR/v3.ext"
  rm -f "$CERT_DIR/server.csr" "$CERT_DIR/v3.ext"
  chmod 600 "$CERT_DIR/server.key"
  echo "[*] Server certificate issued."
  echo "[*] Export this CA to your clients and trust it: $CA_DIR/rootCA.crt"
}

generate_csr_only() {
  local CN="$1"
  echo "[*] Generating CSR for CN=$CN (no signing performed)."
  openssl genrsa -out "$CERT_DIR/server.key" 2048
  openssl req -new -key "$CERT_DIR/server.key" -out "$CERT_DIR/server.csr" -subj "/CN=$CN"
  echo "[*] CSR ready at: $CERT_DIR/server.csr"
  echo "    Sign this CSR with your offline CA, save the cert as $CERT_DIR/server.crt and restart camstack.service"
}
