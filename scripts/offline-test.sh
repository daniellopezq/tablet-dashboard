#!/usr/bin/env bash
# Offline test switch: make the running dashboard act as if the internet were down, to test the
# local fallbacks. Only the dashboard is affected; the server's real network is untouched.
#   scripts/offline-test.sh on [services] [minutes] [realistic|instant]
#       services: comma list of ask,voice,weather,maps,calendar (default ask,voice)
#       minutes:  1-120 (default 15); it always switches itself off afterwards
#   scripts/offline-test.sh off
#   scripts/offline-test.sh status
# The admin page (Offline test) does the same thing.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 netsim.py "$@"
