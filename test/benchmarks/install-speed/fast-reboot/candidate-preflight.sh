#!/bin/bash
# Install the ordinary image candidate, then activate this separate dashboard
# variant. The ordinary installer overlay contains the unguarded release helper,
# so these verified replacements must be installed after its extraction.
set -euo pipefail
payload=/usr/local/lib/omarchy-benchmark/fast-reboot
(cd "$payload" && sha256sum --check --strict payload.sha256)
bash "$payload/image-candidate-preflight.sh"
install -m 0755 "$payload/omarchy-release-install-target" /usr/local/bin/omarchy-release-install-target
install -m 0755 "$payload/omarchy-install-dashboard" /usr/local/bin/omarchy-install-dashboard
cmp "$payload/omarchy-release-install-target" /usr/local/bin/omarchy-release-install-target
cmp "$payload/omarchy-install-dashboard" /usr/local/bin/omarchy-install-dashboard
echo 'Pinned PR145 dashboard activated; immediate guest reboot requires successful sync and target release.'
