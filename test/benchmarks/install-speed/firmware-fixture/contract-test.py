#!/usr/bin/env python3
"""Exercise real xorriso replay on a small, deliberately unbootable test ISO."""

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--source", required=True, type=Path)
  parser.add_argument("--xorriso", default="xorriso")
  args = parser.parse_args()
  spec = importlib.util.spec_from_file_location("repack", Path(__file__).with_name("repack-iso.py"))
  repack = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(repack)
  if repack.sha256(args.source) != repack.RELEASE_SHA256:
    parser.error("The metadata fixture must be derived from the pinned release ISO")
  with tempfile.TemporaryDirectory(prefix="omarchy-firmware-contract-", dir="/tmp") as directory:
    work = Path(directory)
    original, replacement = work / "original-initramfs", work / "replacement.img"
    original.write_bytes(b"Unbootable miniature fixture\n")
    replacement.write_bytes(original.read_bytes() + b"Test overlay\n")
    miniature, output = work / "miniature.iso", work / "result.iso"
    command = [args.xorriso, "-indev", str(args.source.resolve()), "-outdev", str(miniature),
      "-boot_image", "any", "replay", "-map", str(original), repack.INITRAMFS_PATH,
      "-map", str(original), "/arch/x86_64/airootfs.sfs",
      "-volume_date", "c", repack.VOLUME_TIME, "-volume_date", "m", repack.VOLUME_TIME,
      "-volume_date", "uuid", repack.VOLUME_TIME, "-commit", "-end"]
    subprocess.run(command, check=True, capture_output=True)
    # Only this in-process test substitutes pins. The production CLI accepts
    # neither arbitrary releases nor alternative source digests.
    repack.RELEASE_SHA256 = repack.sha256(miniature)
    repack.INITRAMFS_SHA256 = repack.sha256(original)
    replacement_hash = repack.sha256(replacement)
    replacement.with_suffix(".img.manifest.json").write_text(json.dumps({
      "original_initramfs_sha256": repack.INITRAMFS_SHA256,
      "output_initramfs_sha256": replacement_hash, "mode": "control"}))
    sys.argv = ["repack-iso.py", "--source", str(miniature), "--initramfs", str(replacement),
      "--output", str(output), "--xorriso", args.xorriso, "--reserve-bytes", "0"]
    repack.main()
    before, after = repack.describe(miniature, args.xorriso), repack.describe(output, args.xorriso)
    for label, offset, error in [
      ("squashfs corruption", after["files"]["/arch/x86_64/airootfs.sfs"][0][1] * 2048,
        "changed release content"),
      ("EFI image corruption", after["boot"][1]["lba"] * 2048 + 1024, "EFI boot image changed"),
    ]:
      with output.open("r+b") as stream:
        stream.seek(offset)
        saved = stream.read(1)
        stream.seek(offset)
        stream.write(bytes([saved[0] ^ 1]))
      try:
        repack.verify(miniature, output, before, after, replacement_hash)
      except ValueError as failure:
        if error not in str(failure):
          raise
      else:
        raise AssertionError(f"Verifier accepted {label}")
      finally:
        with output.open("r+b") as stream:
          stream.seek(offset)
          stream.write(saved)
    manifest = json.loads(output.with_suffix(".iso.manifest.json").read_text())
    print(json.dumps({"status": "contract-passed", "actual_firmware_boot_tested": False,
      "full_size_iso_repacked": False, "miniature_bytes": output.stat().st_size,
      "regular_files_verified": len(manifest["files"]), "efi_boot_images": manifest["efi_boot_images"],
      "corruption_rejected": ["squashfs", "EFI image"], "volume": manifest["volume"]}))


if __name__ == "__main__":
  main()
