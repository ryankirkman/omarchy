#!/usr/bin/env python3
"""Stage PR145's release-gated guest reboot as a separate benchmark variant."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

PIN = "dbffaa6c65344d644627a023c28661e08382b8fa"
SOURCE_PREFIX = "configs/airootfs/usr/local/bin/"
SOURCE_SHA256 = {
  "omarchy-install-dashboard": "4871faded220498542e1d01a0cbae3f98c21ea5b4eea6bab94fa9e62b415ad89",
  "omarchy-release-install-target": "b8503f3aabd572441a43a8412251767d51a410a6604ad2b31b31bdb8909af97b",
}
HERE = Path(__file__).resolve().parent
PAYLOAD_PATH = Path("usr/local/lib/omarchy-benchmark/fast-reboot")


def digest(data):
  return hashlib.sha256(data).hexdigest()


def read_sources(checkout):
  result = {}
  for name, expected in SOURCE_SHA256.items():
    data = subprocess.check_output(["git", "-C", str(checkout), "show", f"{PIN}:{SOURCE_PREFIX}{name}"])
    if digest(data) != expected:
      raise ValueError(f"Pinned upstream source mismatch: {name}")
    result[name] = data
  result["LICENSE"] = subprocess.check_output(["git", "-C", str(checkout), "show", f"{PIN}:LICENSE"])
  return result


def guard_sync(source):
  # The upstream helper uses set -uo pipefail, so a failed bare sync does not
  # prevent its final exit 0. Preserve its release protocol, failing closed on
  # either sync invocation before permitting the dashboard's immediate reboot.
  if source.count(b"\nsync\n") != 2:
    raise ValueError("Pinned release helper must contain exactly two expected sync calls")
  return source.replace(b"\nsync\n", b"\nsync || exit 1\n")


def prepare(checkout, base_payload, output):
  manifest_path = output.with_name(output.name + ".manifest.json")
  if output.exists() or manifest_path.exists():
    raise ValueError("Fast reboot payload requires fresh output paths")
  sources = read_sources(checkout)
  sources["omarchy-release-install-target"] = guard_sync(sources["omarchy-release-install-target"])
  entries = []
  for source in base_payload.rglob("*"):
    if source.is_symlink() or not (source.is_file() or source.is_dir()):
      raise ValueError("Base payload must contain only regular files/directories")
  shutil.copytree(base_payload, output)
  destination = output / PAYLOAD_PATH
  destination.mkdir(parents=True)
  sources["image-candidate-preflight.sh"] = (HERE.parent / "image/candidate-preflight.sh").read_bytes()
  for name, data in sources.items():
    path = destination / name
    path.write_bytes(data)
    path.chmod(0o644 if name == "LICENSE" else 0o755)
    entries.append({"path": str(path.relative_to(output)), "sha256": digest(data),
                    "upstream_sha256": SOURCE_SHA256.get(name)})
  (destination / "payload.sha256").write_text("".join(
    f"{row['sha256']}  {Path(row['path']).name}\n" for row in entries))
  manifest = {
    "schema_version": 1, "variant": "image-no-package-prefetch-fast-reboot",
    "upstream_commit": PIN, "upstream_pr": "https://github.com/omacom-io/omarchy-iso/pull/145",
    "upstream_credit": "Anton Hvornum; pinned Omarchy ISO PR145 dashboard and release helper (MIT)",
    "changes_to_upstream": ["Make both release-helper sync failures abort the release"],
    "guest_reboot_only": True, "host_reset_used": False, "files": entries,
  }
  manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
  return manifest


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--iso-source", type=Path, required=True)
  parser.add_argument("--base-payload", type=Path, required=True)
  parser.add_argument("--output", type=Path, required=True)
  args = parser.parse_args()
  print(json.dumps(prepare(args.iso_source, args.base_payload, args.output), indent=2))


if __name__ == "__main__":
  main()
