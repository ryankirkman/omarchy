#!/bin/bash
# Use with overlay/make-initramfs.py --preflight. The payload must install
# activate-installer-overlay.sh at the path below before the live root starts.
set -euo pipefail
media=/run/omarchy-fast-image
device=/dev/disk/by-label/OMARCHY_FAST_IMAGE
udevadm settle --timeout=30
[[ -b $device ]] || { echo "Missing supplementary image media: $device" >&2; exit 1; }
mkdir -p "$media"
mount -t iso9660 -o ro "$device" "$media"
bash /usr/local/lib/omarchy-benchmark/activate-installer-overlay.sh "$media"
