#!/bin/bash
# Disposable VM only. Adapted from omacom/omarchy-iso PR #145's
# builder/build-root-image.sh at dbffaa6c65344d644627a023c28661e08382b8fa.
# Build with the official ISO's immutable offline packages, not today's mirrors.
# The raw disk is compressed by the HOST after this VM has shut down.
set -euo pipefail

if (( $# != 3 )); then
  echo "Usage: $0 <extracted-builder-bundle> <baseline-manifests> <small-output-directory>" >&2
  exit 2
fi
bundle=$(realpath "$1")
baseline=$(realpath "$2")
output=$(realpath -m "$3")
device=/dev/disk/by-id/virtio-OMARCHY_IMAGE_BUILD
mirror=/var/cache/omarchy/mirror/offline
subvolume=omarchy-root

[[ $(id -u) == 0 ]] || { echo 'Must run as root inside the disposable VM' >&2; exit 1; }
[[ $(systemd-detect-virt --vm) =~ ^(qemu|kvm)$ ]] || { echo 'Requires a disposable QEMU VM' >&2; exit 1; }
[[ -b $device ]] || { echo "Missing dedicated build disk: $device" >&2; exit 1; }
[[ -f $mirror/offline.db ]] || { echo 'Boot the official ISO live environment first' >&2; exit 1; }
[[ -f $baseline/package-manifest.txt && -f $baseline/package-explicit.txt ]] || {
  echo 'Requires baseline pacman -Q and pacman -Qqe output' >&2; exit 1;
}
[[ ! -e $output ]] || {
  echo 'Build output already exists; use a fresh directory to avoid stale success metadata' >&2; exit 1;
}
[[ -z $(lsblk -nro MOUNTPOINTS "$device" | tr -d '[:space:]') ]] || {
  echo 'Build disk or child is mounted; refusing to format it' >&2; exit 1;
}
[[ -z $(wipefs --no-act --noheadings --output TYPE "$device") ]] || {
  echo 'Build disk has existing signatures; requires a fresh blank benchmark disk' >&2; exit 1;
}
[[ $(blockdev --getsize64 "$device") -ge 12884901888 ]] || {
  echo 'Build disk must have at least 12 GiB virtual capacity' >&2; exit 1;
}
for command in pacstrap btrfs mkfs.btrfs pacman python; do
  command -v "$command" >/dev/null
done

mkdir -p "$output"
work=$(mktemp -d /run/omarchy-root-build.XXXXXX)
mount_dir=$work/mnt
root=$mount_dir/$subvolume
masked_hooks=()
stop_agents() {
  gpgconf --homedir "$root/etc/pacman.d/gnupg" --kill all 2>/dev/null || true
}
cleanup() {
  local status=$?
  set +e
  stop_agents
  if mountpoint -q "$mount_dir"; then
    umount -R "$mount_dir" || echo "WARNING: build filesystem remains mounted at $mount_dir" >&2
  fi
  for hook in "${masked_hooks[@]}"; do
    rm -f "/etc/pacman.d/hooks/$hook"
    if [[ -e $work/hooks/$hook || -L $work/hooks/$hook ]]; then
      mv "$work/hooks/$hook" "/etc/pacman.d/hooks/$hook"
    fi
  done
  # Keep diagnostics and the raw disk on failure. No raw image is erased here.
  return "$status"
}
trap cleanup EXIT

# Use the ISO's package list and package names. Its runtime can differ from
# the checked-out Omarchy tree; never silently substitute that newer version.
python "$bundle/select-image-packages.py" "$bundle" /usr/share/omarchy-iso >"$output/image-targets.txt"
mapfile -t packages <"$output/image-targets.txt"
cat >"$work/pacman.conf" <<EOF
[options]
Architecture = auto
CheckSpace
CacheDir = $mirror
SigLevel = Never
LocalFileSigLevel = Never
[offline]
Server = file://$mirror
EOF
# The outer ISO is verified against the official release checksum before boot.
# SigLevel=Never is the official offline-repository policy, not a new bypass.

mkdir -p /etc/pacman.d/hooks "$work/hooks" "$mount_dir"
for hook in 60-mkinitcpio-remove.hook 60-limine-mkinitcpio-remove-pre.hook 80-limine-efi-deploy.hook 90-limine-mkinitcpio-remove-post.hook 90-mkinitcpio-install.hook; do
  path=/etc/pacman.d/hooks/$hook
  if [[ -L $path && $(readlink "$path") == /dev/null ]]; then
    continue
  fi
  if [[ -e $path || -L $path ]]; then
    mv "$path" "$work/hooks/$hook"
  fi
  ln -s /dev/null "$path"
  masked_hooks+=("$hook")
done

mkfs.btrfs -q -L omarchy-root-image "$device"
mount -o "${OMARCHY_IMAGE_COMPRESSION:-compress-force=zstd:15}" "$device" "$mount_dir"
btrfs subvolume create "$root"
pacstrap -C "$work/pacman.conf" -c -G -M "$root" "${packages[@]}"
stop_agents
rm -rf "$root/etc/pacman.d/gnupg"
: >"$root/etc/machine-id"
# openssh package should not seed keys, but an image must never share them.
rm -f "$root"/etc/ssh/ssh_host_*_key "$root"/etc/ssh/ssh_host_*_key.pub
# Keep package-owned log directories; Qk must still validate the baked image.
# Empty unowned directories (including build-machine journal IDs) can go.
python - "$root" <<'PY'
import sys
from pathlib import Path
root = Path(sys.argv[1])
owned = set()
for record in (root / "var/lib/pacman/local").glob("*/files"):
    text = record.read_text()
    if "%FILES%\n" in text:
        owned.update(text.split("%FILES%\n", 1)[1].split("\n\n", 1)[0].splitlines())
for path in sorted((root / "var/log").rglob("*"), key=lambda p: len(p.parts), reverse=True):
    relative = path.relative_to(root).as_posix()
    if path.is_dir() and not path.is_symlink():
        if relative + "/" not in owned:
            path.rmdir()
    elif relative != "var/log/pacman.log":
        if relative in owned:
            if path.is_file() and not path.is_symlink():
                path.write_bytes(b"")
        else:
            path.unlink()
PY
rm -rf "$root/var/cache/pacman/pkg"/*
# filesystem owns this file. Restore its packaged default, not the live guest's
# resolver copied by pacstrap; deleting it leaves an incomplete package.
rm -f "$root/etc/resolv.conf"
cp -a "$root/usr/share/factory/etc/resolv.conf" "$root/etc/resolv.conf"

pacman --root "$root" -Q | LC_ALL=C sort >"$output/image-package-manifest.txt"
python "$bundle/validate-image-manifest.py" "$baseline" "$output"
mapfile -t installed < <(pacman --root "$root" -Qq)
mapfile -t explicit <"$output/image-explicit-packages.txt"
pacman --root "$root" -D --asdeps "${installed[@]}"
if (( ${#explicit[@]} > 0 )); then
  pacman --root "$root" -D --asexplicit "${explicit[@]}"
fi
pacman --root "$root" -Qqe | LC_ALL=C sort >"$output/image-explicit-packages-actual.txt"
cmp "$output/image-explicit-packages.txt" "$output/image-explicit-packages-actual.txt"
pacman --root "$root" -Qk >"$output/image-package-files.txt" 2>&1

sync
btrfs property set -ts "$root" ro true
read -r minimum _ < <(btrfs inspect-internal min-dev-size "$mount_dir")
alignment=$((256 * 1024 * 1024))
headroom=$((512 * 1024 * 1024))
image_size=$(((minimum + headroom + alignment - 1) / alignment * alignment))
btrfs filesystem resize "$image_size" "$mount_dir"
# QEMU must expose discard=unmap on this sparse build disk.
fstrim "$mount_dir"
sync
stop_agents
umount -R "$mount_dir"
btrfs check --readonly --check-data-csum "$device" >"$output/btrfs-check.txt" 2>&1
printf '%s\n' "$image_size" >"$output/raw-image-size.txt"
printf '%s\n' "${OMARCHY_IMAGE_COMPRESSION:-compress-force=zstd:15}" >"$output/btrfs-compression.txt"
printf '%s\n' 'BUILD_COMPLETE' >"$output/build-status.txt"
echo "Build complete: $image_size virtual bytes. Copy small output files, shut down this VM, then compress the raw disk on the host."
