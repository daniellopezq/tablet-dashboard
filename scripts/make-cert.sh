#!/usr/bin/env bash
# Create a private certificate authority and an https certificate for the dashboard.
#   scripts/make-cert.sh           create (refuses to overwrite existing files)
#   scripts/make-cert.sh --force   replace them
# Private keys stay in data/tls/ (mode 600, never served). The CA's PUBLIC certificate is
# copied to web/dashboard-ca.crt so the tablet can optionally install it as trusted.
set -euo pipefail
cd "$(dirname "$0")/.."
DIR=data/tls
IP=${DASHBOARD_IP:-$(hostname -I | awk '{print $1}')}   # override: DASHBOARD_IP=192.168.1.20 scripts/make-cert.sh
HOST=$(hostname)
umask 077
mkdir -p "$DIR"
chmod 700 data "$DIR"

if [ -f "$DIR/server.crt" ] && [ "${1:-}" != "--force" ]; then
  echo "Certificates already exist in $DIR (use --force to replace)."
  exit 0
fi

openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 3650 \
  -keyout "$DIR/ca.key" -out "$DIR/ca.crt" \
  -subj "/CN=Tablet Dashboard local CA" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null

openssl req -newkey rsa:2048 -sha256 -nodes \
  -keyout "$DIR/server.key" -out "$DIR/server.csr" \
  -subj "/CN=$IP" 2>/dev/null

cat > "$DIR/server.ext" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=IP:$IP,IP:127.0.0.1,DNS:localhost,DNS:$HOST
EOF

openssl x509 -req -in "$DIR/server.csr" -CA "$DIR/ca.crt" -CAkey "$DIR/ca.key" -CAcreateserial \
  -days 1825 -sha256 -extfile "$DIR/server.ext" -out "$DIR/server.crt" 2>/dev/null
rm -f "$DIR/server.csr" "$DIR/server.ext"
chmod 600 "$DIR"/*

# Public CA certificate only (no key) for optional installation on the tablet.
umask 022
cp "$DIR/ca.crt" web/dashboard-ca.crt
chmod 644 web/dashboard-ca.crt

openssl x509 -in "$DIR/server.crt" -noout -subject -enddate -ext subjectAltName
echo "SHA-256 fingerprint of the CA (compare on the tablet if you install it):"
openssl x509 -in "$DIR/ca.crt" -noout -fingerprint -sha256
echo "Done. Set \"https_port\": 8443 in config.json and restart the dashboard."
