#!/usr/bin/python3
"""Finish the guest-built raw filesystem with native-host QEMU compression."""
import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("raw", type=Path)
parser.add_argument("build_output", type=Path)
parser.add_argument("destination", type=Path)
parser.add_argument("--qemu-img", default="qemu-img")
args = parser.parse_args()
if (args.build_output / "build-status.txt").read_text().strip() != "BUILD_COMPLETE":
    parser.error("guest filesystem did not finish validation")
size = int((args.build_output / "raw-image-size.txt").read_text())
if size < 1024**3 or size > args.raw.stat().st_size or size % (256 * 1024**2):
    parser.error("invalid guest filesystem size")
if args.destination.exists():
    parser.error("destination already exists; use a fresh output path")
args.destination.parent.mkdir(parents=True, exist_ok=True)
workers = min(len(os.sched_getaffinity(0)), 16)


def qemu(*command):
    subprocess.run([args.qemu_img, *map(str, command)], check=True)


# Unlike truncate(2), qemu-img resize acquires QEMU's image write lock. An
# active build VM causes this to fail rather than shrinking its mounted disk.
qemu("resize", "--shrink", "-f", "raw", args.raw, size)
qemu("convert", "-c", "-f", "raw", "-O", "qcow2", "-o", "cluster_size=1048576,lazy_refcounts=on,compression_type=zstd", "-m", workers, args.raw, args.destination)
qemu("check", "-f", "qcow2", args.destination)
qemu("compare", "-f", "raw", "-F", "qcow2", args.raw, args.destination)
digest = hashlib.sha256()
with args.destination.open("rb") as source:
    while chunk := source.read(8 * 1024**2):
        digest.update(chunk)
args.destination.with_suffix(args.destination.suffix + ".sha256").write_text(f"{digest.hexdigest()}  {args.destination.name}\n")
manifest = {"schema_version": 1, "upstream_commit": "dbffaa6c65344d644627a023c28661e08382b8fa", "virtual_bytes": size, "file_bytes": args.destination.stat().st_size, "sha256": digest.hexdigest(), "compression": "Btrfs " + (args.build_output / "btrfs-compression.txt").read_text().strip() + "; qcow2 zstd 1MiB clusters", "qemu_version": subprocess.check_output([args.qemu_img, "--version"], text=True).splitlines()[0], "verification": ["guest Btrfs data checksums", "qemu-img check", "qemu-img compare raw versus qcow2", "SHA-256"], "image_package_delta": json.loads((args.build_output / "image-package-delta.json").read_text())}
args.destination.with_suffix(args.destination.suffix + ".json").write_text(json.dumps(manifest, indent=2) + "\n")
print(json.dumps(manifest, indent=2))
