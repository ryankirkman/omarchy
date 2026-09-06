#!/usr/bin/env python3
"""Bind the optimized logger only for the pinned serial system finalizer."""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import stat
import subprocess


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
spec = importlib.util.spec_from_file_location("logging_bind_patch", HERE / "patch.py")
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)
PIN = "dbffaa6c65344d644627a023c28661e08382b8fa"
BASE_VARIANT = "image-no-package-prefetch-fast-reboot-early-verify-direct-restore-overlap"
LOGGING_SCOPE = "serial-system-finalizer-only"
PAYLOAD_PATH = Path("usr/local/lib/omarchy-benchmark/logging-bind")
ANIMATION_PATH = Path("usr/local/lib/omarchy-benchmark/animation-overlap/omarchy-install-dashboard")
DASHBOARD_SHA256 = "33a77a5ed86df5d6d14b39a027237b888c65b4dbaa7855c10e4a78865f89111d"
BASES = {
  BASE_VARIANT: {
    "phases_path": "usr/local/lib/omarchy-benchmark/localdb-overlap/phases_impl.py",
    "preflight_sha256": "510a502d0f4388a43b137e2e22e1b65eaa10a271b299c8c31131365bbbd94b5a",
    "component": "foreground-animation-overlap",
    "phases_manifest_key": None,
    "variant": BASE_VARIANT + "-logging-bind",
  },
  BASE_VARIANT + "-firewall": {
    "phases_path": "usr/local/lib/omarchy-benchmark/firewall-overlap/phases_impl.py",
    "preflight_sha256": "e58a6ed01ff672115ebf8a95be4cb40b622e17c1066393a4aa7954d8378a44eb",
    "component": "firewall-overlap",
    "phases_manifest_key": "firewall_phases_sha256",
    "variant": BASE_VARIANT + "-firewall-logging",
  },
}
STAGED_MODES = {
  "activation.json": 0o644,
  "base-preflight.sh": 0o755,
  "guard.py": 0o644,
  "LICENSE.omarchy": 0o644,
  "LICENSE.omarchy-iso": 0o644,
  "logging.sh": 0o644,
  "phases_impl.py": 0o644,
}


def digest(data):
  return hashlib.sha256(data).hexdigest()


def no_symlinks(path):
  for component in (path, *path.parents):
    if component.is_symlink():
      raise ValueError(f"Payload path traverses a symlink: {component}")


def regular(path):
  no_symlinks(path)
  if not path.is_file():
    raise ValueError(f"Payload source must be a regular file: {path}")
  return path.read_bytes()


def inventory(directory):
  no_symlinks(directory)
  if not directory.is_dir():
    raise ValueError("Base payload must be a regular directory")
  rows = []
  for path in sorted(directory.rglob("*")):
    name = path.relative_to(directory).as_posix()
    if path.is_symlink() or not (path.is_file() or path.is_dir()) or "\n" in name or "\r" in name:
      raise ValueError("Payload requires regular files/directories with unambiguous names")
    if path.is_file():
      rows.append({"path": name, "sha256": digest(path.read_bytes()),
        "mode": oct(stat.S_IMODE(path.stat().st_mode)), "bytes": path.stat().st_size})
  return rows


def directory_modes(directory):
  return {path.relative_to(directory).as_posix(): stat.S_IMODE(path.stat().st_mode)
    for path in (directory, *directory.rglob("*")) if path.is_dir()}


def prepare(checkout, base_payload, base_preflight, output):
  manifest_path = output.with_name(output.name + ".manifest.json")
  for path in (output, manifest_path):
    no_symlinks(path)
    if path.resolve().is_relative_to(base_payload.resolve()):
      raise ValueError("Logging bind output paths must be outside the base payload")
    if path.exists():
      raise ValueError("Logging bind requires fresh output paths")
  base_manifest_path = base_payload.with_name(base_payload.name + ".manifest.json")
  base_manifest_bytes = regular(base_manifest_path)
  base_manifest = json.loads(base_manifest_bytes)
  before = inventory(base_payload)
  before_directories = directory_modes(base_payload)
  base_variant = base_manifest.get("variant")
  if (type(base_manifest.get("schema_version")) is not int or base_manifest.get("schema_version") != 1
      or base_manifest.get("upstream_commit") != PIN or base_variant not in BASES
      or base_variant not in patch.SOURCE_SHA256S or base_manifest.get("files") != before):
    raise ValueError("Logging bind requires the complete, exactly pinned overlap payload")
  base = BASES[base_variant]
  if base_manifest.get("component") != base["component"]:
    raise ValueError("Base manifest component differs from its exact approved variant")
  if (base["phases_manifest_key"] is not None
      and base_manifest.get(base["phases_manifest_key"]) != patch.SOURCE_SHA256S[base_variant]):
    raise ValueError("Base manifest phases hash differs from its exact approved variant")
  base_preflight_bytes = regular(base_preflight)
  if (digest(base_preflight_bytes) != base["preflight_sha256"]
      or base_manifest.get("preflight_sha256") != digest(base_preflight_bytes)):
    raise ValueError("Base preflight differs from the pinned payload provenance")
  if digest(regular(base_payload / ANIMATION_PATH)) != DASHBOARD_SHA256:
    raise ValueError("Logging bind requires the unchanged pinned animation dashboard")
  if (base_variant == BASE_VARIANT and base_manifest.get("dashboard_sha256") != DASHBOARD_SHA256
      or base_manifest.get("dashboard_sha256", DASHBOARD_SHA256) != DASHBOARD_SHA256):
    raise ValueError("Base manifest dashboard hash differs from the pinned animation")
  original = regular(base_payload / base["phases_path"])
  if digest(original) != patch.SOURCE_SHA256S[base_variant]:
    raise ValueError("Base phases differ from the exact variant source")
  if (base_payload / PAYLOAD_PATH).exists():
    raise ValueError("Base payload already contains logging bind staging")
  patched = patch.patch_source(original)
  compile(patched, "logging-bind-phases_impl.py", "exec")
  logger = regular(REPO / "install/helpers/logging.sh")
  if digest(logger) != patch.LOGGER_SHA256:
    raise ValueError("Optimized logger differs from the pinned repository source")
  subprocess.run(["bash", "-n"], input=logger, check=True)
  guard = regular(HERE / "guard.py")
  compile(guard, "logging-bind-guard.py", "exec")
  activation = {
    "schema_version": 1, "base_variant": base_variant, "variant": base["variant"],
    "logging_scope": LOGGING_SCOPE,
    "source_phases_sha256": digest(original), "phases_sha256": digest(patched),
    "base_preflight_sha256": digest(base_preflight_bytes),
    "original_logger_sha256": patch.ORIGINAL_LOGGER_SHA256,
    "logger_sha256": digest(logger), "guard_sha256": digest(guard),
  }
  sources = {
    "activation.json": (json.dumps(activation, indent=2) + "\n").encode(),
    "base-preflight.sh": base_preflight_bytes,
    "guard.py": guard,
    "LICENSE.omarchy": regular(REPO / "LICENSE"),
    "LICENSE.omarchy-iso": subprocess.check_output(["git", "-C", str(checkout), "show", f"{PIN}:LICENSE"]),
    "logging.sh": logger,
    "phases_impl.py": patched,
  }
  shutil.copytree(base_payload, output)
  staged = output / PAYLOAD_PATH
  staged.mkdir(parents=True)
  for name, data in sources.items():
    path = staged / name
    path.write_bytes(data)
    path.chmod(STAGED_MODES[name])
  checksum = staged / "payload.sha256"
  checksum.write_text("".join(f"{digest(data)}  {name}\n" for name, data in sorted(sources.items())))
  checksum.chmod(0o644)
  after = inventory(output)
  inherited_paths = {row["path"] for row in before}
  after_directories = directory_modes(output)
  if ([row for row in after if row["path"] in inherited_paths] != before
      or any(after_directories.get(name) != mode for name, mode in before_directories.items())):
    raise ValueError("Logging bind staging changed inherited bytes or modes")
  manifest = {
    "schema_version": 1, "variant": base["variant"], "component": "private-logging-bind",
    "base_variant": base_variant, "upstream_commit": PIN,
    **{key: activation[key] for key in ("logging_scope", "source_phases_sha256", "phases_sha256",
      "base_preflight_sha256", "original_logger_sha256", "logger_sha256", "guard_sha256")},
    "source_phases_path": base["phases_path"], "dashboard_sha256": DASHBOARD_SHA256,
    "base_payload_manifest_sha256": digest(base_manifest_bytes),
    "preflight_sha256": digest(regular(HERE / "preflight.sh")),
    "supplemental_image_changed": False, "target_package_files_changed": False,
    "activation_order": ["verify complete logging bind staging", "inherited preflight exactly once",
      "verify exact inherited live phases", "install verified logging bind phases"],
    "changes": ["Bind the pinned optimized logger read-only only inside the serial system finalizer's private namespace",
      "Keep every other setup call on the original logger path",
      "Retain original package-owned logger bytes and unchanged localdb source guards"],
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
