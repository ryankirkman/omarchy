#!/bin/bash
# Activate each existing layer once, then replace only the exact direct phases.
set -euo pipefail
payload=/usr/local/lib/omarchy-benchmark/localdb-overlap
live=/usr/share/omarchy-iso/orchestrator/phases_impl.py
(cd "$payload" && sha256sum --check --strict payload.sha256)
bash "$payload/direct-restore-preflight.sh"
printf '%s  %s\n' '8787646c45b164b4fde2abb894c87ece46e9c8f180ff96fede9ed23b2723a458' "$live" | sha256sum --check --strict
install -m 0644 "$payload/phases_impl.py" "$live"
cmp "$payload/phases_impl.py" "$live"
printf '%s  %s\n' '6914997592990435c723688e594ed189192e961423324b505a66be0de1948128' "$live" | sha256sum --check --strict
echo 'Verified localdb overlap activated; unchanged indexing must join before validation and snapshot creation.'
