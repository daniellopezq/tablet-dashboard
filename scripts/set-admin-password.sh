#!/usr/bin/env bash
# Set or change the admin page password (https://<server>:<https_port>/admin, LAN only).
# The password is typed with echo off and stored only as a scrypt hash in data/admin.json (mode 600).
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
exec python3 admin.py set-password
