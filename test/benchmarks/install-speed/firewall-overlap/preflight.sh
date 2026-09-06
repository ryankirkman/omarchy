#!/bin/bash
# Inherited activation must finish before replacing the pinned localdb phases.
set -euo pipefail
payload=/usr/local/lib/omarchy-benchmark/firewall-overlap
live=/usr/share/omarchy-iso/orchestrator/phases_impl.py
(cd "$payload" && sha256sum --check --strict payload.sha256)
bash "$payload/base-preflight.sh"
printf '%s  %s\n' '6914997592990435c723688e594ed189192e961423324b505a66be0de1948128' "$live" | sha256sum --check --strict
install -m 0644 "$payload/phases_impl.py" "$live"
cmp "$payload/phases_impl.py" "$live"
echo 'Firewall overlap activated; unchanged firewall setup precedes user changes and indexing and must join before validation.'
