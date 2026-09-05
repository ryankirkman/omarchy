#!/usr/bin/python3
"""Hash the embedded root image without extracting a second multi-GB copy."""
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("iso", type=Path)
parser.add_argument("root_image_manifest", type=Path)
parser.add_argument("output", type=Path)
parser.add_argument("--xorriso", default="xorriso")
args = parser.parse_args()
if args.output.exists():
    parser.error("verification output already exists; use a fresh path")
image_path = "/arch/x86_64/omarchy-root.btrfs.qcow2"
reference = json.loads(args.root_image_manifest.read_text())
expected_bytes, expected_sha256 = reference["file_bytes"], reference["sha256"]
if not isinstance(expected_bytes, int) or expected_bytes <= 0 or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
    parser.error("invalid root-image provenance manifest")
# xorriso report_sections differs from report_lba: the byte count belongs to
# EACH extent, so images beyond ISO9660's 4-GiB per-extent limit work as well.
result = subprocess.run([args.xorriso, "-indev", str(args.iso), "-find", image_path, "-exec", "report_sections", "--"], check=True, capture_output=True, text=True)
pattern = re.compile(r"File data lba:\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'" + re.escape(image_path) + "'")
extents = [tuple(map(int, match.groups())) for line in result.stdout.splitlines() if (match := pattern.fullmatch(line))]
extents.sort()
if [entry[0] for entry in extents] != list(range(len(extents))) or not extents:
    parser.error("missing or ambiguous image extents")
if sum(entry[3] for entry in extents) != expected_bytes:
    parser.error("embedded image size differs from the validated build")
iso_bytes = args.iso.stat().st_size
digest = hashlib.sha256()
with args.iso.open("rb") as source:
    for _, lba, blocks, length in extents:
        if length <= 0 or length > blocks * 2048 or lba * 2048 + length > iso_bytes:
            parser.error("invalid or truncated ISO extent")
        source.seek(lba * 2048)
        remaining = length
        while remaining:
            chunk = source.read(min(8 * 1024**2, remaining))
            if not chunk:
                parser.error("unexpected EOF in embedded root image")
            digest.update(chunk)
            remaining -= len(chunk)
if digest.hexdigest() != expected_sha256:
    parser.error("embedded root image checksum mismatch; keep the loose validated image")
iso_digest = hashlib.sha256()
with args.iso.open("rb") as source:
    while chunk := source.read(8 * 1024**2):
        iso_digest.update(chunk)
verification = {"schema_version": 1, "iso_bytes": iso_bytes, "iso_sha256": iso_digest.hexdigest(), "root_image_path": image_path, "root_image_bytes": expected_bytes, "root_image_sha256": digest.hexdigest(), "extent_count": len(extents), "verified_against": reference, "status": "verified"}
args.output.write_text(json.dumps(verification, indent=2) + "\n")
print(json.dumps(verification, indent=2))
