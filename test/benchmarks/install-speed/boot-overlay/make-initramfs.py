#!/usr/bin/env python3
"""Append a pre-autoinstall live-root hook to an immutable ArchISO initramfs."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import stat
import subprocess


def sha256(data):
  return hashlib.sha256(data).hexdigest()


def cpio_entries(data, offset=0):
  """Read one newc archive without extracting paths onto the host."""
  entries = {}
  while data[offset:offset + 6] in (b"070701", b"070702"):
    header = data[offset:offset + 110]
    if len(header) != 110:
      raise ValueError("Truncated cpio header")
    fields = [int(header[i:i + 8], 16) for i in range(6, 110, 8)]
    size, namesize = fields[6], fields[11]
    if not namesize:
      raise ValueError("Empty cpio filename")
    start = offset + 110
    raw_name = data[start:start + namesize]
    if len(raw_name) != namesize or raw_name[-1:] != b"\0":
      raise ValueError("Truncated cpio filename")
    name = raw_name[:-1].decode()
    start = (start + namesize + 3) & ~3
    end = start + size
    if end > len(data):
      raise ValueError("Truncated cpio member")
    entries[name.removeprefix("./")] = (fields[1], data[start:end])
    offset = (end + 3) & ~3
    if name == "TRAILER!!!":
      return entries, offset
  raise ValueError("Expected a complete newc cpio archive")


def initramfs_files(data):
  """The official image has an early newc archive followed by zstd newc."""
  files = {}
  offset = 0
  while offset < len(data):
    if data[offset] == 0:
      offset += 1
      continue
    if data[offset:offset + 6] in (b"070701", b"070702"):
      entries, offset = cpio_entries(data, offset)
      files.update(entries)
    elif data[offset:offset + 4] == b"\x28\xb5\x2f\xfd":
      # zstd is a host build dependency, never installed into the target system.
      unpacked = subprocess.run(["zstd", "-dc"], input=data[offset:], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
      files.update(initramfs_files(unpacked))
      break
    elif data[offset:offset + 2] == b"\x1f\x8b":
      files.update(initramfs_files(gzip.decompress(data[offset:])))
      break
    else:
      raise ValueError(f"Unsupported initramfs segment at byte {offset}")
  return files


def make_cpio(files):
  archive = bytearray()
  for inode, (name, (mode, data)) in enumerate([*sorted(files.items()), ("TRAILER!!!", (0, b""))], 1):
    encoded = name.encode() + b"\0"
    fields = [inode, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(encoded), 0]
    archive.extend(b"070701" + "".join(f"{value:08x}" for value in fields).encode())
    archive.extend(encoded)
    archive.extend(b"\0" * (-len(archive) % 4))
    archive.extend(data)
    archive.extend(b"\0" * (-len(archive) % 4))
  archive.extend(b"\0" * (-len(archive) % 512))
  return bytes(archive)


HOOK = b'''# This file is sourced by mkinitcpio's BusyBox ash, not Bash.
copy_benchmark_payload() {
  while IFS= read -r relative; do
    source=/omarchy-benchmark-payload/$relative
    # mkdir -p leaves existing live directory modes (especially /root) intact.
    parent=${relative%/*}
    [ "$parent" != "$relative" ] || parent=.
    mkdir -p "/new_root/$parent" || return 1
    cp -p "$source" "/new_root/$relative" || return 1
  done </omarchy-benchmark-files
}

run_latehook() {
  if [ ! -f /new_root/root/.automated_script.sh ] ||
     [ -e /new_root/root/.automated_script.benchmark-original.sh ]; then
    echo 'OMARCHY BENCHMARK: unexpected live entry point; refusing autoinstall' >/dev/console
    while :; do sleep 60; done
  fi
  if ! mv /new_root/root/.automated_script.sh /new_root/root/.automated_script.benchmark-original.sh ||
     ! copy_benchmark_payload; then
    echo 'OMARCHY BENCHMARK: overlay copy failed; refusing autoinstall' >/dev/console
    while :; do sleep 60; done
  fi
}
'''


EARLY_PREFLIGHT_UNIT = b'''[Unit]
Description=Activate the Omarchy benchmark fixture before console autoinstall
After=local-fs.target
Before=getty@tty1.service

[Service]
Type=oneshot
RemainAfterExit=yes
TimeoutStartSec=300
TimeoutStopSec=30
ExecStart=/usr/local/lib/omarchy-benchmark/run-early-preflight.sh
'''

EARLY_PREFLIGHT_GETTY = b'''[Unit]
Requires=omarchy-benchmark-preflight.service
After=omarchy-benchmark-preflight.service
'''


def early_preflight_script(mode):
  if mode not in ("control", "candidate"):
    raise ValueError("Early preflight supports control and candidate modes only")
  return f'''#!/bin/bash
set -euo pipefail
mkdir -p /run/omarchy-benchmark /var/log
if [[ -e /run/omarchy-benchmark/early-preflight-started || -e /run/omarchy-benchmark/preflight-complete ]]; then
  echo 'Early benchmark activation was already attempted; refusing to activate twice.' >&2
  exit 1
fi
read -r preflight_boot_id </proc/sys/kernel/random/boot_id
record_serial_marker() {{
  if [[ -c /dev/ttyS0 ]]; then
    printf 'OMARCHY_BENCHMARK_EARLY_PREFLIGHT %s %s %s\\n' "$1" "$preflight_boot_id" "$preflight_uptime" >/dev/ttyS0 || true
  fi
}}
read -r preflight_uptime preflight_idle </proc/uptime
printf '%s %s\\n' "$preflight_boot_id" "$preflight_uptime" >/run/omarchy-benchmark/early-preflight-started
record_serial_marker started
if /usr/local/lib/omarchy-benchmark/preflight.sh >/var/log/omarchy-benchmark-preflight.log 2>&1; then
  read -r preflight_uptime preflight_idle </proc/uptime
  printf '%s %s\\n' "$preflight_boot_id" "$preflight_uptime" >/run/omarchy-benchmark/early-preflight-finished
  record_serial_marker finished
  printf '%s\\n' '{mode}' >/run/omarchy-benchmark/preflight-complete.tmp
  mv /run/omarchy-benchmark/preflight-complete.tmp /run/omarchy-benchmark/preflight-complete
else
  preflight_status=$?
  cat /var/log/omarchy-benchmark-preflight.log >&2
  echo 'Early benchmark preflight failed; autoinstall has not started.' >&2
  exit "$preflight_status"
fi
'''.encode()


def wrapper(mode, disable_package_prefetch=False, early_preflight=False):
  prefetch = "export OMARCHY_NO_PREFETCH=1\n" if disable_package_prefetch else ""
  if early_preflight:
    if mode not in ("control", "candidate"):
      raise ValueError("Early preflight supports control and candidate modes only")
    return f'''#!/bin/bash
set -euo pipefail
[[ $(tty) == /dev/tty1 ]] || exit 0
if ! systemctl is-active --quiet omarchy-benchmark-preflight.service ||
   ! preflight_exec_status=$(systemctl show omarchy-benchmark-preflight.service -p ExecMainStatus --value) ||
   [[ $preflight_exec_status != 0 ]] ||
   [[ ! -s /run/omarchy-benchmark/early-preflight-started || ! -s /run/omarchy-benchmark/early-preflight-finished ]] ||
   [[ ! -f /run/omarchy-benchmark/preflight-complete ]] ||
   ! preflight_complete_mode=$(cat /run/omarchy-benchmark/preflight-complete) ||
   [[ $preflight_complete_mode != '{mode}' ]]; then
  echo 'Early benchmark preflight has no successful service and marker; refusing autoinstall.' >&2
  exit 1
fi
{prefetch}\
exec /root/.automated_script.benchmark-original.sh
'''.encode()
  return f'''#!/bin/bash
set -euo pipefail
[[ $(tty) == /dev/tty1 ]] || exit 0
mkdir -p /run/omarchy-benchmark /var/log
if ! /usr/local/lib/omarchy-benchmark/preflight.sh >/var/log/omarchy-benchmark-preflight.log 2>&1; then
  cat /var/log/omarchy-benchmark-preflight.log
  echo 'Benchmark preflight failed; autoinstall has not started.' >&2
  exit 1
fi
printf '%s\\n' '{mode}' >/run/omarchy-benchmark/preflight-complete
{prefetch}\
if [[ '{mode}' == 'builder' ]]; then
  echo 'Benchmark builder is ready. Autoinstall is disabled for this boot.'
else
  exec /root/.automated_script.benchmark-original.sh
fi
'''.encode()


def build(original, mode, preflight, payload=None, disable_package_prefetch=False, early_preflight=False):
  source_files = initramfs_files(original)
  config = source_files.get("config", (0, b""))[1]
  init = source_files.get("init", (0, b""))[1]
  if b'LATEHOOKS=' not in config or b'"$mount_handler" /new_root' not in init or b"run_hookfunctions 'run_latehook'" not in init:
    raise ValueError("Input is not a compatible mkinitcpio ArchISO initramfs")
  if b"omarchy_benchmark" in config:
    raise ValueError("Input already contains a benchmark hook")
  contents = {
    "root/.automated_script.sh": (stat.S_IFREG | 0o755, wrapper(mode, disable_package_prefetch, early_preflight)),
    "usr/local/lib/omarchy-benchmark/preflight.sh": (stat.S_IFREG | 0o755, preflight),
  }
  if early_preflight:
    contents.update({
      "etc/systemd/system/omarchy-benchmark-preflight.service": (stat.S_IFREG | 0o644, EARLY_PREFLIGHT_UNIT),
      "etc/systemd/system/getty@tty1.service.d/50-omarchy-benchmark-preflight.conf": (stat.S_IFREG | 0o644, EARLY_PREFLIGHT_GETTY),
      "usr/local/lib/omarchy-benchmark/run-early-preflight.sh": (stat.S_IFREG | 0o755, early_preflight_script(mode)),
    })
  if payload:
    for path in sorted(payload.rglob("*")):
      if path.is_symlink() or not (path.is_file() or path.is_dir()):
        raise ValueError(f"Payload accepts regular files/directories only: {path}")
      name = path.relative_to(payload).as_posix()
      if "\n" in name or "\r" in name:
        raise ValueError(f"Payload filename must not contain newlines: {name!r}")
      if name in contents or name in ("root/.automated_script.benchmark-original.sh", "usr/local/lib/omarchy-benchmark/manifest.json"):
        raise ValueError(f"Reserved payload path: {name}")
      if path.is_file():
        contents[name] = (stat.S_IFREG | stat.S_IMODE(path.stat().st_mode), path.read_bytes())
  metadata = {
    "schema_version": 1,
    "mode": mode,
    "disable_package_prefetch": disable_package_prefetch,
    "original_initramfs_sha256": sha256(original),
    "original_config_sha256": sha256(config),
    "hook_sha256": sha256(HOOK),
    "payload": [{"path": name, "mode": oct(stat.S_IMODE(mode_bits)), "sha256": sha256(data)} for name, (mode_bits, data) in sorted(contents.items())],
  }
  if early_preflight:
    metadata["early_preflight"] = True
  contents["usr/local/lib/omarchy-benchmark/manifest.json"] = (stat.S_IFREG | 0o644, (json.dumps(metadata, indent=2) + "\n").encode())
  files = {
    "config": (stat.S_IFREG | 0o644, config + b'\nLATEHOOKS="$LATEHOOKS omarchy_benchmark"\n'),
    "hooks/omarchy_benchmark": (stat.S_IFREG | 0o755, HOOK),
    "omarchy-benchmark-files": (stat.S_IFREG | 0o644, ("\n".join(sorted(contents)) + "\n").encode()),
  }
  files.update({f"omarchy-benchmark-payload/{name}": value for name, value in contents.items()})
  for name in list(files):
    for parent in Path(name).parents:
      if str(parent) != ".":
        files.setdefault(parent.as_posix(), (stat.S_IFDIR | 0o755, b""))
  appended = make_cpio(files)
  combined = original + b"\0" * (-len(original) % 4) + appended
  metadata.update({"appended_archive_bytes": len(appended), "appended_archive_sha256": sha256(appended), "output_initramfs_sha256": sha256(combined)})
  return combined, metadata


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--initramfs", required=True, type=Path)
  parser.add_argument("--output", required=True, type=Path)
  parser.add_argument("--mode", choices=("control", "candidate", "builder"), required=True)
  parser.add_argument("--preflight-script", type=Path)
  parser.add_argument("--payload-dir", type=Path)
  parser.add_argument("--disable-package-prefetch", action="store_true", help="Experimental: export upstream OMARCHY_NO_PREFETCH=1; image verification remains enabled")
  parser.add_argument("--early-preflight", action="store_true", help="Experimental control/candidate: activate once during live systemd boot before tty1; retain original image verification gate")
  parser.add_argument("--expected-initramfs-sha256")
  args = parser.parse_args()
  if args.initramfs.resolve() == args.output.resolve():
    parser.error("Output must not overwrite the original initramfs")
  if args.output.exists():
    parser.error("Output already exists; choose a fresh path")
  if args.mode != "control" and not args.preflight_script:
    parser.error("Candidate and builder modes require --preflight-script")
  if args.early_preflight and args.mode == "builder":
    parser.error("Early preflight supports control and candidate modes only")
  original = args.initramfs.read_bytes()
  if args.expected_initramfs_sha256 and sha256(original) != args.expected_initramfs_sha256:
    parser.error("Original initramfs checksum mismatch")
  preflight = args.preflight_script.read_bytes() if args.preflight_script else b"#!/bin/bash\nset -euo pipefail\necho 'Matched control preflight'\n"
  combined, metadata = build(original, args.mode, preflight, args.payload_dir, args.disable_package_prefetch, args.early_preflight)
  args.output.parent.mkdir(parents=True, exist_ok=True)
  args.output.write_bytes(combined)
  manifest = args.output.with_suffix(args.output.suffix + ".manifest.json")
  manifest.write_text(json.dumps(metadata, indent=2) + "\n")
  print(json.dumps({"initramfs": str(args.output.resolve()), "manifest": str(manifest.resolve()), **metadata}, indent=2))


if __name__ == "__main__":
  main()
