#!/usr/bin/env python3
"""Replace the benchmark initramfs while retaining the release firmware boot chain."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess


RELEASE_SHA256 = "2ef8e624aa1bec7e277e28056b8535a6c9373ba48d7ede3f1a01cb6d2373cfb8"
INITRAMFS_SHA256 = "6e3e15b983da69df4e18df2f1489fa854980b395b28546355d0f6dc13914694e"
INITRAMFS_PATH = "/arch/boot/x86_64/initramfs-linux-t2.img"
BIOS_PATH = "/boot/syslinux/isolinux.bin"
VOLUME_TIME = "2026083103245800"
SECTION = re.compile(r"File data lba:\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'([^']+)'")
BOOT = re.compile(r"El Torito boot img\s*:\s*(\d+)\s+(BIOS|UEFI)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\d+)\s+(\d+)")


def sha256(path):
  with path.open("rb") as stream:
    return hashlib.file_digest(stream, "sha256").hexdigest()


def describe(iso, xorriso):
  result = subprocess.run([xorriso, "-indev", str(iso), "-pvd_info", "-report_el_torito", "plain",
    "-find", "/", "-type", "f", "-exec", "report_sections", "--"], check=True, capture_output=True, text=True)
  output = result.stdout + "\n" + result.stderr
  files, boot, volume = {}, [], {}
  for line in output.splitlines():
    if match := SECTION.fullmatch(line):
      section, lba, blocks, length = map(int, match.groups()[:4])
      files.setdefault(match[5], []).append((section, lba, blocks, length))
    elif match := BOOT.fullmatch(line):
      boot.append({"index": int(match[1]), "platform": match[2], "bootable": match[3],
        "emulation": match[4], "load_segment": match[5], "partition_type": match[6],
        "load_sectors": int(match[7]), "lba": int(match[8])})
    elif ":" in line:
      key, value = line.split(":", 1)
      if key.strip() in {"Volume Id", "Publisher Id", "App Id", "Creation Time", "Modif. Time"}:
        volume[key.strip()] = value.strip()
  if not files or [entry["platform"] for entry in boot] != ["BIOS", "UEFI"]:
    raise ValueError("Expected the release's BIOS and UEFI El Torito entries")
  size = iso.stat().st_size
  for name, sections in files.items():
    sections.sort()
    if [section[0] for section in sections] != list(range(len(sections))):
      raise ValueError(f"Missing or ambiguous ISO extents: {name}")
    for _, lba, blocks, length in sections:
      if length > blocks * 2048 or lba * 2048 + length > size:
        raise ValueError(f"Invalid or truncated ISO extent: {name}")
  return {"files": files, "boot": boot, "volume": volume}


def hash_sections(stream, sections, normalize_bios=False):
  digest, offset = hashlib.sha256(), 0
  for _, lba, _, length in sections:
    stream.seek(lba * 2048)
    remaining = length
    while remaining:
      data = stream.read(min(8 * 1024**2, remaining))
      if not data:
        raise ValueError("Unexpected EOF in ISO extent")
      if normalize_bios and offset < 64:
        # ISOLINUX's boot-info-table stores layout addresses and a checksum.
        # Replaying El Torito legitimately rewrites bytes 8 through 63 only.
        data = bytearray(data)
        start, end = max(8 - offset, 0), min(64 - offset, len(data))
        data[start:end] = b"\0" * max(0, end - start)
      digest.update(data)
      remaining -= len(data)
      offset += len(data)
  return {"bytes": offset, "sha256": digest.hexdigest()}


def verify(source, output, before, after, initramfs_sha256):
  if before["volume"] != after["volume"]:
    raise ValueError("Repacking changed the release volume identity")
  if before["files"].keys() != after["files"].keys():
    raise ValueError("Repacking changed the ISO file inventory")
  normalize = lambda row: {key: value for key, value in row.items() if key != "lba"}
  if list(map(normalize, before["boot"])) != list(map(normalize, after["boot"])):
    raise ValueError("Repacking changed the BIOS/UEFI boot entry parameters")
  checks = {}
  with source.open("rb") as original, output.open("rb") as result:
    for name in sorted(before["files"]):
      expected = hash_sections(original, before["files"][name], name == BIOS_PATH)
      actual = hash_sections(result, after["files"][name], name == BIOS_PATH)
      if name == INITRAMFS_PATH:
        if expected["sha256"] != INITRAMFS_SHA256 or actual["sha256"] != initramfs_sha256:
          raise ValueError("Embedded initramfs digest differs from the validated input")
      elif expected != actual:
        raise ValueError(f"Repacking changed release content: {name}")
      checks[name] = {**actual, "comparison": "replacement" if name == INITRAMFS_PATH else
        "boot-info-table-normalized" if name == BIOS_PATH else "byte-identical"}
    efi = []
    for previous, current in zip(before["boot"], after["boot"]):
      if previous["platform"] != "UEFI":
        continue
      length = previous["load_sectors"] * 512
      expected = hash_sections(original, [(0, previous["lba"], (length + 2047) // 2048, length)])
      actual = hash_sections(result, [(0, current["lba"], (length + 2047) // 2048, length)])
      if expected != actual:
        raise ValueError("Embedded FAT EFI boot image changed")
      efi.append({**actual, "comparison": "byte-identical"})
  return checks, efi


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--source", required=True, type=Path)
  parser.add_argument("--initramfs", required=True, type=Path)
  parser.add_argument("--output", required=True, type=Path)
  parser.add_argument("--xorriso", default="xorriso")
  parser.add_argument("--reserve-bytes", type=int, default=8 * 1024**3,
    help="free space to retain after creating the full ISO (default: 8 GiB)")
  args = parser.parse_args()
  source, initramfs, output = (path.resolve() for path in (args.source, args.initramfs, args.output))
  manifest = output.with_suffix(output.suffix + ".manifest.json")
  if output.exists() or manifest.exists() or args.reserve_bytes < 0:
    parser.error("Use a fresh output path and a nonnegative space reserve")
  if not output.parent.is_dir():
    parser.error("Create the output directory before repacking")
  if sha256(source) != RELEASE_SHA256:
    parser.error("Source is not the pinned, unmodified Omarchy 4.0.2 release")
  overlay_path = initramfs.with_suffix(initramfs.suffix + ".manifest.json")
  overlay = json.loads(overlay_path.read_text())
  initramfs_hash = sha256(initramfs)
  if (overlay.get("original_initramfs_sha256") != INITRAMFS_SHA256 or
      overlay.get("output_initramfs_sha256") != initramfs_hash or
      overlay.get("mode") not in {"control", "candidate", "builder"}):
    parser.error("Initramfs does not match its benchmark-overlay provenance")
  expected_size = source.stat().st_size + initramfs.stat().st_size + 64 * 1024**2
  if shutil.disk_usage(output.parent).free < expected_size + args.reserve_bytes:
    parser.error("Insufficient space for the ISO plus the requested reserve")
  before = describe(source, args.xorriso)
  if (before["volume"].get("Creation Time") != VOLUME_TIME or
      before["volume"].get("Modif. Time") != VOLUME_TIME):
    parser.error("Unexpected source volume timestamps")
  command = [args.xorriso, "-indev", str(source), "-outdev", str(output),
    "-boot_image", "any", "replay", "-map", str(initramfs), INITRAMFS_PATH,
    "-volume_date", "c", VOLUME_TIME, "-volume_date", "m", VOLUME_TIME,
    "-volume_date", "uuid", VOLUME_TIME, "-commit", "-end"]
  # Incomplete or failed outputs are retained for diagnosis, never marked valid.
  with output.with_suffix(output.suffix + ".repack.log").open("x") as log:
    subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT)
  after = describe(output, args.xorriso)
  files, efi = verify(source, output, before, after, initramfs_hash)
  record = {"schema_version": 1, "status": "verified-content-not-yet-boot-tested",
    "fixture": "release-firmware-grub-with-benchmark-initramfs", "source_sha256": RELEASE_SHA256,
    "output_sha256": sha256(output), "output_bytes": output.stat().st_size,
    "initramfs_sha256": initramfs_hash, "overlay_manifest_sha256": sha256(overlay_path),
    "overlay": overlay, "volume": after["volume"], "boot_entries": after["boot"],
    "efi_boot_images": efi, "files": files, "repack_argv": command,
    "kernel_command_line_changed": False, "standalone_installed_boot_validated": False}
  manifest.write_text(json.dumps(record, indent=2) + "\n")
  print(json.dumps({key: record[key] for key in ("status", "output_sha256", "output_bytes", "fixture")}))


if __name__ == "__main__":
  main()
