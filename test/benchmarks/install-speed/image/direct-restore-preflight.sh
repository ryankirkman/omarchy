#!/bin/bash
# Keep one ordinary supplementary ISO. Activate it and the guarded dashboard
# first, then replace only the exact verified phases source in the live root.
set -euo pipefail
payload=/usr/local/lib/omarchy-benchmark/direct-restore
live=/usr/share/omarchy-iso/orchestrator/phases_impl.py
(cd "$payload" && sha256sum --check --strict payload.sha256)
bash "$payload/fast-reboot-preflight.sh"
printf '%s  %s\n' '8c802ec9ad8b94478ad16d4ca434fa6197741b4d1b3195b0a78d0c876b8682bf' "$live" | sha256sum --check --strict
install -m 0644 "$payload/phases_impl.py" "$live"
cmp "$payload/phases_impl.py" "$live"
printf '%s  %s\n' '8787646c45b164b4fde2abb894c87ece46e9c8f180ff96fede9ed23b2723a458' "$live" | sha256sum --check --strict
echo 'Verified direct restore activated after the ordinary overlay; full image verification remains required.'
