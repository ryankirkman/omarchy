#!/usr/bin/env python3
"""Append a pinned foreground animation experiment to an existing image payload."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import stat
import subprocess

from dashboard_patch import PIN, SOURCE_PATH, SOURCE_SHA256, patch_source


HERE = Path(__file__).resolve().parent
PAYLOAD_PATH = Path("usr/local/lib/omarchy-benchmark/animation-overlap")


def digest(data):
  return hashlib.sha256(data).hexdigest()


def inventory(directory):
  rows = []
  for path in sorted(directory.rglob("*")):
    name = path.relative_to(directory).as_posix()
    if path.is_symlink() or not (path.is_file() or path.is_dir()) or "\n" in name or "\r" in name:
      raise ValueError("Payload requires regular files/directories with unambiguous names")
    if path.is_file():
      rows.append({"path": name, "sha256": digest(path.read_bytes()),
        "mode": oct(stat.S_IMODE(path.stat().st_mode)), "bytes": path.stat().st_size})
  return rows


def prepare(checkout, base_payload, base_preflight, output):
  manifest_path = output.with_name(output.name + ".manifest.json")
  if output.exists() or manifest_path.exists():
    raise ValueError("Animation payload requires fresh output paths")
  base_manifest_path = base_payload.with_name(base_payload.name + ".manifest.json")
  base_manifest_bytes = base_manifest_path.read_bytes()
  base_manifest = json.loads(base_manifest_bytes)
  before = inventory(base_payload)
  if base_manifest.get("upstream_commit") != PIN or base_manifest.get("files") != before:
    raise ValueError("Base payload differs from its complete pinned inventory")
  if base_preflight.is_symlink() or not base_preflight.is_file():
    raise ValueError("Base preflight must be a regular file")
  base_preflight_bytes = base_preflight.read_bytes()
  if base_manifest.get("preflight_sha256") != digest(base_preflight_bytes):
    raise ValueError("Base preflight differs from the base payload's provenance")
  if (base_payload / PAYLOAD_PATH).exists():
    raise ValueError("Base payload already contains animation overlap")
  original = subprocess.check_output(["git", "-C", str(checkout), "show", f"{PIN}:{SOURCE_PATH}"])
  patched = patch_source(original)
  subprocess.run(["bash", "-n"], input=patched, check=True)
  sources = {
    "omarchy-install-dashboard": (patched, 0o755),
    "base-preflight.sh": (base_preflight_bytes, 0o755),
    "LICENSE": (subprocess.check_output(["git", "-C", str(checkout), "show", f"{PIN}:LICENSE"]), 0o644),
  }
  shutil.copytree(base_payload, output)
  staged = output / PAYLOAD_PATH
  staged.mkdir(parents=True)
  for name, (data, mode) in sources.items():
    path = staged / name
    path.write_bytes(data)
    path.chmod(mode)
  checksum = staged / "payload.sha256"
  checksum.write_text("".join(f"{digest(data)}  {name}\n" for name, (data, _) in sorted(sources.items())))
  checksum.chmod(0o644)
  after = inventory(output)
  if [row for row in after if row["path"] in {item["path"] for item in before}] != before:
    raise ValueError("Animation staging changed inherited payload bytes or metadata")
  manifest = {
    "schema_version": 1,
    "variant": "image-no-package-prefetch-fast-reboot-early-verify-direct-restore-overlap",
    "component": "foreground-animation-overlap",
    "base_variant": base_manifest["variant"],
    "upstream_commit": PIN, "upstream_source_path": SOURCE_PATH,
    "upstream_source_sha256": SOURCE_SHA256, "dashboard_sha256": digest(patched),
    "upstream_credit": "Anton Hvornum; Omarchy ISO PR145 dashboard and complete logo effect (MIT)",
    "base_payload_manifest_sha256": digest(base_manifest_bytes),
    "base_preflight_sha256": digest(base_preflight_bytes),
    "preflight_sha256": digest((HERE / "preflight.sh").read_bytes()),
    "supplemental_image_changed": False, "installer_phases_changed": False,
    "activation_order": ["inherited preflight exactly once", "verify original pinned live dashboard", "install verified animation dashboard"],
    "changes": ["Run the unchanged full foreground effect once during the existing finalization child window when observed",
      "Use neutral in-progress text until ordinary child success and target release",
      "Retain the post-release effect when the finalization window was not observed",
      "Record candidate-only same-boot uptime and boot-ID effect markers inside the full host clock"],
    "files": after,
  }
  manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
  return manifest


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--iso-source", type=Path, required=True)
  parser.add_argument("--base-payload", type=Path, required=True)
  parser.add_argument("--base-preflight", type=Path, required=True)
  parser.add_argument("--output", type=Path, required=True)
  args = parser.parse_args()
  print(json.dumps(prepare(args.iso_source, args.base_payload, args.base_preflight, args.output), indent=2))


if __name__ == "__main__":
  main()
