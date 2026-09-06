#!/bin/bash
# Apply the benchmark overlay before starting any configurator or installer.
# This does NOT start the installer or select/wipe a target disk.
set -euo pipefail
if (( $# != 1 )); then
  echo "Usage: $0 <read-only-image-media-mount>" >&2
  exit 2
fi
media=$(realpath "$1")
[[ $(id -u) == 0 && $(systemd-detect-virt --vm) =~ ^(qemu|kvm)$ ]] || {
  echo 'Only supported inside the disposable QEMU benchmark VM' >&2; exit 1;
}
[[ -e /run/archiso/bootmnt/arch/x86_64/airootfs.sfs ]] || {
  echo 'Requires official ISO live boot with copytoram=n' >&2; exit 1;
}
[[ ,$(findmnt -no OPTIONS --target "$media"), == *,ro,* ]] || {
  echo 'Image source must be mounted read-only' >&2; exit 1;
}
[[ -f $media/installer-overlay.tar && -f $media/installer-overlay.tar.sha256 && -f $media/arch/x86_64/omarchy-root.btrfs.qcow2.sha256 ]] || {
  echo 'Image media is incomplete' >&2; exit 1;
}
if ! command -v qemu-img >/dev/null; then
  [[ -f $media/qemu-img-live.tar && -f $media/qemu-img-live.tar.sha256 ]] || {
    echo 'Candidate live environment needs qemu-img; supply a pinned live binary bundle' >&2; exit 1;
  }
  (cd "$media" && sha256sum --check --strict qemu-img-live.tar.sha256)
  tar -xf "$media/qemu-img-live.tar" -C /
fi
qemu-img --version
if pgrep -f '^python -m orchestrator.main$' >/dev/null; then
  echo 'Installer is already running; overlay must precede installer start' >&2
  exit 1
fi
(cd "$media" && sha256sum --check --strict installer-overlay.tar.sha256)
tar -xf "$media/installer-overlay.tar" -C /
# The live squashfs is already open and mounted. This read-only bind places
# the qcow2 at upstream's unmodified path without unpacking/rebuilding the ISO.
mount --bind "$media/arch/x86_64" /run/archiso/bootmnt/arch/x86_64
mount -o remount,bind,ro /run/archiso/bootmnt/arch/x86_64
image_bytes=$(stat -c %s /run/archiso/bootmnt/arch/x86_64/omarchy-root.btrfs.qcow2)
mkdir -p /etc/systemd/system/omarchy-root-image-verify.service.d
printf '[Service]\nTimeoutStartSec=%s\n' "$((image_bytes / (2 * 1024 * 1024) + 600))" \
  >/etc/systemd/system/omarchy-root-image-verify.service.d/50-size-timeout.conf
systemctl daemon-reload
systemctl start --no-block omarchy-root-image-verify.service
echo 'Pinned image installer activated; upstream verification must succeed before its disk preparation phase.'
