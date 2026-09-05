#!/bin/bash

set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/base-test.sh"

python3 - "$ROOT" <<'PYTEST'
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import uuid

spec = importlib.util.spec_from_file_location("comparison", Path(sys.argv[1]) / "test/benchmarks/compare-installs.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def rejects(function, description):
  try:
    function()
  except (ValueError, KeyError, FileNotFoundError):
    return
  raise AssertionError(description)


def fixture_identity(name):
  def digest(suffix):
    return hashlib.sha256((name + suffix).encode()).digest()
  return {
    "machine_id": digest("machine").hex()[:32],
    "pacman_master_key_fingerprint": digest("pacman").hex()[:40].upper(),
    "btrfs_uuid": str(uuid.UUID(bytes=digest("btrfs")[:16])),
    "ssh_host_key_fingerprints": ["SHA256:" + base64.b64encode(digest(kind)).decode().rstrip("=")
                                  for kind in ("rsa", "ed25519")],
  }


# These invented durations and digests exercise the acceptance contract only.
# They are not measurements and must never appear in performance reports.
with tempfile.TemporaryDirectory() as directory:
  root = Path(directory)
  manifest = {
    "status": "installed-and-booted", "mode": "install", "fresh_target": True, "fresh_nvram": True,
    "measurement_interrupted": False,
    "accelerator": "tcg", "cpu_count": 4, "memory_mib": 8192,
    "disk_format": "qcow2", "disk_virtual_bytes": 40 * 1024**3,
    "disk_cache": "writeback", "iso_cache": "writeback", "qemu_version": "fixture",
    "encryption": False, "filesystem": "btrfs compress=zstd",
    "cidata_configuration_sha256": "a" * 64,
    "iso_sha256": "b" * 64, "test_overlay_sha256": None,
    "direct_kernel_boot": True, "direct_kernel_sha256": "1" * 64,
    "direct_initrd_sha256": "2" * 64, "direct_kernel_command_line": "archisobasedir=arch",
    "reboot_strategy": "qemu-no-reboot-then-disk",
    "first_installed_ssh_wall_s": 96, "last_failed_installed_ssh_probe_started_wall_s": 94,
    "last_failed_installed_ssh_wall_s": 95, "readiness_poll_uncertainty_s": 2,
    "readiness_poll_interval_s": 30,
    "media_cache_preconditioning": "sha256-read-iso-then-extra-media-in-array-order-then-kernel-then-initrd-before-vm-start",
    "extra_media": [
      {"path": "/tmp/root-image.iso", "sha256": "6" * 64, "drive_id": "root-image", "format": "raw",
       "cache": "writeback", "interface": "ide-cd", "device": "ide-cd,drive=root-image,bus=ide.1", "readonly": True},
      {"path": "/tmp/fixtures.iso", "sha256": "7" * 64, "drive_id": "fixtures", "format": "raw",
       "cache": "writeback", "interface": "virtio-blk-pci", "device": "virtio-blk-pci,drive=fixtures", "readonly": True},
    ],
  }
  timing = {"current_phase": "Installation complete", "total_phases": 1,
            "phases": [{"name": "Install", "status": "ok", "elapsed": 12}],
            "installed_packages": 2, "started_at": 100, "finished_at": 112}
  validation = {"booted_installed_root": True, "package_files_exit_status": 0}
  identity = fixture_identity("read-run-fixture")
  artifacts = {
    "manifest.json": manifest,
    "install-timing.json": timing,
    "validation.json": validation,
    "identity.json": identity,
    "package-manifest.txt": "kernel 1.0\nshell 2.0\n",
    "package-explicit.txt": "shell\n",
  }

  def write_artifacts(changes=None):
    for name, value in {**artifacts, **(changes or {})}.items():
      path = root / name
      if value is None:
        path.unlink(missing_ok=True)
      else:
        path.write_text(value if isinstance(value, str) else json.dumps(value))

  def rejects_artifacts(changes, description):
    write_artifacts(changes)
    rejects(lambda: module.read_run(root), description)

  write_artifacts()
  run = module.read_run(root)
  assert run["elapsed"] == 12
  assert run["elapsed_clock"] == "guest-wall-clock"
  assert run["guest_wall_clock_delta_seconds"] == 12
  assert run["explicit_packages"] == ["shell"]
  assert run["identity"]["machine_id"] == identity["machine_id"]
  assert run["ssh_poll_uncertainty_seconds"] == 2  # Never substitute the nominal 30 seconds.
  for key, value in (("booted_installed_root", False), ("package_files_exit_status", 1),
                     ("package_files_exit_status", False)):
    rejects_artifacts({"validation.json": {**validation, key: value}}, "invalid installation accepted")
  for key, value in (("status", "timeout"), ("measurement_interrupted", True),
                     ("mode", "builder"), ("media_cache_preconditioning", "unknown"),
                     ("extra_media", None), ("extra_media", {}),
                     ("measurement_interrupted", 0), ("fresh_target", False), ("fresh_nvram", False),
                     ("cidata_configuration_sha256", "unknown"), ("test_overlay_sha256", "unknown"),
                     ("iso_sha256", "unknown"), ("direct_kernel_boot", 1),
                     ("direct_kernel_sha256", None), ("direct_initrd_sha256", "unknown"),
                     ("direct_kernel_command_line", None), ("reboot_strategy", ""),
                     ("first_installed_ssh_wall_s", float("inf")), ("first_installed_ssh_wall_s", 0),
                     ("first_installed_ssh_wall_s", 90), ("last_failed_installed_ssh_probe_started_wall_s", -1),
                     ("last_failed_installed_ssh_wall_s", 93), ("last_failed_installed_ssh_wall_s", 97),
                     ("readiness_poll_uncertainty_s", -1), ("readiness_poll_uncertainty_s", float("nan")),
                     ("readiness_poll_uncertainty_s", 30)):
    rejects_artifacts({"manifest.json": {**manifest, key: value}}, f"invalid {key} accepted")
  for key in ("measurement_interrupted", "encryption", "filesystem", "cidata_configuration_sha256", "test_overlay_sha256",
              "direct_kernel_boot", "direct_kernel_sha256", "direct_initrd_sha256", "direct_kernel_command_line",
              "reboot_strategy", "first_installed_ssh_wall_s", "last_failed_installed_ssh_probe_started_wall_s",
              "readiness_poll_uncertainty_s", "mode", "extra_media", "media_cache_preconditioning"):
    incomplete = {name: value for name, value in manifest.items() if name != key}
    rejects_artifacts({"manifest.json": incomplete}, f"missing {key} accepted")
  for text in ("", "kernel 1.0\nkernel 2.0\n", "kernel\n", "kernel 1.0 extra\n"):
    rejects_artifacts({"package-manifest.txt": text}, "malformed package inventory accepted")
  for text in (None, "shell\nshell\n", "absent\n", "shell explicit\n", " shell\n", "\n"):
    rejects_artifacts({"package-explicit.txt": text}, "missing or malformed package reasons accepted")
  for key, value in (("current_phase", "Installing"), ("total_phases", 2), ("installed_packages", 3),
                     ("phases", []), ("started_at", float("nan")), ("finished_at", float("inf")),
                     ("finished_at", True), ("finished_at", 100), ("finished_at", 99)):
    rejects_artifacts({"install-timing.json": {**timing, key: value}}, "incomplete or invalid timing accepted")
  for value in (0, -1, True, "12", float("nan"), float("inf")):
    rejects_artifacts({"install-timing.json": {**timing, "duration_seconds": value}},
                      "invalid authoritative monotonic duration silently fell back to wall clock")
  for finished_at in (99, 500):
    write_artifacts({"install-timing.json": {**timing, "finished_at": finished_at, "duration_seconds": 12.5}})
    monotonic_run = module.read_run(root)
    assert monotonic_run["elapsed"] == 12.5
    assert monotonic_run["elapsed_clock"] == "guest-monotonic"
    assert monotonic_run["guest_wall_clock_delta_seconds"] == finished_at - timing["started_at"]
  rejects_artifacts({"install-timing.json": {**timing, "duration_seconds": 12.5, "started_at": float("nan")}},
                    "monotonic duration hid missing valid start/end timestamps")
  for phase in ({"name": "Install", "status": "failed", "elapsed": 12},
                {"name": "Install", "status": "ok", "elapsed": -1},
                {"name": "Install", "status": "ok", "elapsed": float("nan")},
                {"name": "Install", "status": "ok", "elapsed": True},
                {"name": "", "status": "ok", "elapsed": 12}):
    rejects_artifacts({"install-timing.json": {**timing, "phases": [phase]}}, "failed or malformed phase accepted")
  rejects_artifacts({"install-timing.json": {**timing, "total_phases": 2, "phases": timing["phases"] * 2}},
                    "duplicate phase names silently collapsed")
  firmware = {**manifest, "direct_kernel_boot": False, "direct_kernel_sha256": None,
              "direct_initrd_sha256": None, "direct_kernel_command_line": None,
              "reboot_strategy": "guest-firmware-reboot"}
  write_artifacts({"manifest.json": firmware})
  assert not module.read_run(root)["boot_fixture"]["direct_kernel_boot"]
  rejects_artifacts({"manifest.json": {**firmware, "direct_kernel_sha256": "1" * 64}},
                    "firmware boot accepted inconsistent direct kernel metadata")
  for missing in (None, {}, []):
    rejects_artifacts({"identity.json": missing}, "missing machine identity evidence accepted")
  for key in identity:
    incomplete = {name: value for name, value in identity.items() if name != key}
    rejects_artifacts({"identity.json": incomplete}, f"missing {key} identity accepted")
  for key, value in (("machine_id", ""), ("machine_id", "uninitialized"), ("machine_id", "0" * 32),
                     ("machine_id", "1" * 31), ("pacman_master_key_fingerprint", ""),
                     ("pacman_master_key_fingerprint", "0" * 40), ("pacman_master_key_fingerprint", "unknown"),
                     ("btrfs_uuid", ""), ("btrfs_uuid", "00000000-0000-0000-0000-000000000000"),
                     ("btrfs_uuid", identity["btrfs_uuid"].replace("-", "")),
                     ("ssh_host_key_fingerprints", []), ("ssh_host_key_fingerprints", "SHA256:unknown"),
                     ("ssh_host_key_fingerprints", ["MD5:00:11"]),
                     ("ssh_host_key_fingerprints", ["SHA256:" + "A" * 43]),
                     ("ssh_host_key_fingerprints", ["SHA256:" + "B" * 43]),
                     ("ssh_host_key_fingerprints", identity["ssh_host_key_fingerprints"] * 2)):
    rejects_artifacts({"identity.json": {**identity, key: value}}, f"invalid {key} accepted")
  # Case variations cannot hide duplicate hexadecimal public identifiers.
  write_artifacts({"identity.json": {**identity, "btrfs_uuid": identity["btrfs_uuid"].upper(),
                                     "pacman_master_key_fingerprint": identity["pacman_master_key_fingerprint"].lower()}})
  assert module.read_run(root)["identity"] == run["identity"]
  for key, value in (("readonly", False), ("readonly", 1), ("sha256", "unknown"), ("format", ""),
                     ("path", "relative.iso"), ("cache", ""), ("interface", ""), ("device", ""), ("drive_id", 1)):
    media = copy.deepcopy(manifest["extra_media"])
    media[0][key] = value
    rejects_artifacts({"manifest.json": {**manifest, "extra_media": media}}, f"invalid supplementary media {key} accepted")
  for key in manifest["extra_media"][0]:
    media = copy.deepcopy(manifest["extra_media"])
    del media[0][key]
    rejects_artifacts({"manifest.json": {**manifest, "extra_media": media}}, f"missing supplementary media {key} accepted")
  media = copy.deepcopy(manifest["extra_media"])
  media[1]["drive_id"] = media[0]["drive_id"]
  rejects_artifacts({"manifest.json": {**manifest, "extra_media": media}}, "duplicate supplementary drive ID accepted")
  write_artifacts({"manifest.json": {**manifest, "extra_media": []}})
  assert module.read_run(root)["fixture"]["extra_media_topology"] == []


def sample(name, seconds):
  result = copy.deepcopy(run)
  result.update(directory=name, elapsed=seconds, boot_to_ssh_seconds=seconds * 8,
                ssh_readiness_lower_bound_seconds=seconds * 8 - 2, identity=fixture_identity(name))
  for index, medium in enumerate(result["extra_media"]):
    medium["path"] = f"/tmp/{name}/media-{index}.iso"
  return result


baseline = [sample(f"baseline-{index}", 12) for index in range(3)]
candidate = [sample(f"candidate-{index}", 5) for index in range(3)]
# Different candidate inputs are expected; each group still needs one revision.
for result in candidate:
  result.update(iso_sha256="c" * 64, test_overlay_sha256="d" * 64, direct_initrd_sha256="3" * 64)
  result["elapsed_clock"] = "guest-monotonic"
  for index, medium in enumerate(result["extra_media"]):
    medium["sha256"] = hashlib.sha256(f"candidate-media-{index}".encode()).hexdigest()
comparison = module.compare(baseline, candidate)
assert comparison["twofold_target_verified_for_this_fixture"]
assert comparison["guest_installer"]["twofold_verified_for_this_fixture"]
assert comparison["guest_installer"]["clock_sources"]["baseline"] == ["guest-wall-clock"] * 3
assert comparison["guest_installer"]["clock_sources"]["candidate"] == ["guest-monotonic"] * 3
assert comparison["host_boot_to_installed_ssh"]["conservative_speedup_lower_bound"] == 94 / 40
assert comparison["explicit_package_count"] == 1
assert comparison["distinct_installation_identities_verified"]
assert comparison["runs"][3]["test_overlay_sha256"] == "d" * 64
assert comparison["runs"][0]["test_overlay_sha256"] is None
assert not module.compare(baseline[:1], candidate[:1])["twofold_target_verified_for_this_fixture"]
assert not module.compare(baseline[:2], candidate)["twofold_target_verified_for_this_fixture"]
rejects(lambda: module.compare([], candidate), "empty baseline accepted")
rejects(lambda: module.compare(baseline, []), "empty candidate accepted")
rejects(lambda: module.compare(baseline, baseline), "same runs accepted twice")
for key in ("machine_id", "pacman_master_key_fingerprint", "btrfs_uuid"):
  for source in (baseline[0], candidate[1]):
    altered = copy.deepcopy(candidate)
    altered[0]["identity"][key] = source["identity"][key]
    rejects(lambda: module.compare(baseline, altered), f"shared {key} accepted")
for source in (baseline[0], candidate[1]):
  altered = copy.deepcopy(candidate)
  # Detect even one stale key when the other host keys were regenerated.
  altered[0]["identity"]["ssh_host_key_fingerprints"][0] = source["identity"]["ssh_host_key_fingerprints"][0]
  rejects(lambda: module.compare(baseline, altered), "partially cloned SSH host keys accepted")
for key, value in (("packages", ["kernel 1.0"]), ("packages", ["kernel 2.0", "shell 2.0"]),
                   ("explicit_packages", ["kernel", "shell"]), ("iso_sha256", "e" * 64),
                   ("test_overlay_sha256", None), ("direct_initrd_sha256", "4" * 64)):
  altered = copy.deepcopy(candidate)
  altered[0][key] = value
  rejects(lambda: module.compare(baseline, altered), f"incomparable {key} accepted")
for key, value in (("accelerator", "kvm"), ("disk_cache", "none"), ("encryption", True),
                   ("filesystem", "ext4"), ("cidata_configuration_sha256", "f" * 64),
                   ("media_cache_preconditioning", "cold-cache")):
  altered = copy.deepcopy(candidate)
  # Entire groups may differ in image, but never in these fixture settings.
  for result in altered:
    result["fixture"][key] = value
  rejects(lambda: module.compare(baseline, altered), f"incomparable {key} accepted")
altered = copy.deepcopy(candidate)
altered[0]["extra_media"][0]["sha256"] = "9" * 64
rejects(lambda: module.compare(baseline, altered), "changing supplementary media within a revision accepted")
for key, value in (("cache", "none"), ("interface", "virtio-blk-pci"),
                   ("device", "ide-cd,drive=root-image,bus=ide.0"), ("readonly", False)):
  altered = copy.deepcopy(candidate)
  for result in altered:
    result["fixture"]["extra_media_topology"][0][key] = value
  rejects(lambda: module.compare(baseline, altered), f"different supplementary {key} accepted")
altered = copy.deepcopy(candidate)
for result in altered:
  result["extra_media"].reverse()
  result["fixture"]["extra_media_topology"].reverse()
rejects(lambda: module.compare(baseline, altered), "different supplementary media and preconditioning order accepted")
for key, value in (("direct_kernel_boot", False), ("direct_kernel_sha256", "5" * 64),
                   ("direct_kernel_command_line", "archisobasedir=arch skip-work=1"),
                   ("reboot_strategy", "guest-firmware-reboot")):
  altered = copy.deepcopy(candidate)
  for result in altered:
    result["boot_fixture"][key] = value
  comparison = module.compare(baseline, altered)
  assert comparison["guest_installer"]["twofold_verified_for_this_fixture"]
  assert not comparison["host_boot_to_installed_ssh"]["comparable"]
  assert comparison["host_boot_to_installed_ssh"]["conservative_speedup_lower_bound"] is None
  assert not comparison["twofold_target_verified_for_this_fixture"]
# Moving work before the guest installer clock cannot satisfy the whole goal.
altered = copy.deepcopy(candidate)
for result in altered:
  result.update(boot_to_ssh_seconds=90, ssh_readiness_lower_bound_seconds=88)
comparison = module.compare(baseline, altered)
assert comparison["guest_installer"]["twofold_verified_for_this_fixture"]
assert not comparison["twofold_target_verified_for_this_fixture"]
# Even an observed median above 2x is insufficient when actual polling
# uncertainty makes a sub-2x improvement consistent with the measurements.
altered = copy.deepcopy(baseline)
for result in altered:
  result.update(ssh_readiness_lower_bound_seconds=79, ssh_poll_uncertainty_seconds=17)
comparison = module.compare(altered, candidate)
assert comparison["host_boot_to_installed_ssh"]["median_observed_speedup"] > 2
assert not comparison["twofold_target_verified_for_this_fixture"]
altered = copy.deepcopy(candidate)
altered[-1]["elapsed"] = 7
comparison = module.compare(baseline, altered)
assert comparison["guest_installer"]["median_speedup"] > 2
assert not comparison["guest_installer"]["twofold_verified_for_this_fixture"]
PYTEST

pass "install comparison rejects interrupted, incomplete, repeated, or incomparable results"
