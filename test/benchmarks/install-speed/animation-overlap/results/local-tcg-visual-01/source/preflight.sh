#!/bin/bash
# Apply only after the inherited image/direct-write/finalization activation.
set -euo pipefail
payload=/usr/local/lib/omarchy-benchmark/animation-overlap
live=/usr/local/bin/omarchy-install-dashboard
(cd "$payload" && sha256sum --check --strict payload.sha256)
bash "$payload/base-preflight.sh"
printf '%s  %s\n' '4871faded220498542e1d01a0cbae3f98c21ea5b4eea6bab94fa9e62b415ad89' "$live" | sha256sum --check --strict
install -m 0755 "$payload/omarchy-install-dashboard" "$live"
cmp "$payload/omarchy-install-dashboard" "$live"
echo 'Foreground animation overlap activated; ordinary installer success and target release still gate completion.'
