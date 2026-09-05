#!/bin/bash

set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/base-test.sh"

python3 - "$ROOT" <<'PYTEST'
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile

spec = importlib.util.spec_from_file_location("comparison", Path(sys.argv[1]) / "test/benchmarks/compare-installs.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def rejects(function, description):
  try:
    function()
  except (ValueError, KeyError, FileNotFoundError):
    return
  raise AssertionError(description)


# These invented durations and digests exercise the acceptance contract only.
# They are not measurements and must never appear in performance reports.
with tempfile.TemporaryDirectory() as directory:
  root = Path(directory)
  manifest = {
    "status": "installed-and-booted", "fresh_target": True, "fresh_nvram": True,
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
  }
  timing = {"current_phase": "Installation complete", "total_phases": 1,
            "phases": [{"name": "Install", "status": "ok", "elapsed": 12}],
            "installed_packages": 2, "started_at": 100, "finished_at": 112}
  validation = {"booted_installed_root": True, "package_files_exit_status": 0}
  artifacts = {
    "manifest.json": manifest,
    "install-timing.json": timing,
    "validation.json": validation,
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
  assert run["explicit_packages"] == ["shell"]
  assert run["ssh_poll_uncertainty_seconds"] == 2  # Never substitute the nominal 30 seconds.
  for key, value in (("booted_installed_root", False), ("package_files_exit_status", 1),
                     ("package_files_exit_status", False)):
    rejects_artifacts({"validation.json": {**validation, key: value}}, "invalid installation accepted")
  for key, value in (("status", "timeout"), ("measurement_interrupted", True),
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
              "readiness_poll_uncertainty_s"):
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


def sample(name, seconds):
  result = copy.deepcopy(run)
  result.update(directory=name, elapsed=seconds, boot_to_ssh_seconds=seconds * 8,
                ssh_readiness_lower_bound_seconds=seconds * 8 - 2)
  return result


baseline = [sample(f"baseline-{index}", 12) for index in range(3)]
candidate = [sample(f"candidate-{index}", 5) for index in range(3)]
# Different candidate inputs are expected; each group still needs one revision.
for result in candidate:
  result.update(iso_sha256="c" * 64, test_overlay_sha256="d" * 64, direct_initrd_sha256="3" * 64)
comparison = module.compare(baseline, candidate)
assert comparison["twofold_target_verified_for_this_fixture"]
assert comparison["guest_installer"]["twofold_verified_for_this_fixture"]
assert comparison["host_boot_to_installed_ssh"]["conservative_speedup_lower_bound"] == 94 / 40
assert comparison["explicit_package_count"] == 1
assert comparison["runs"][3]["test_overlay_sha256"] == "d" * 64
assert comparison["runs"][0]["test_overlay_sha256"] is None
assert not module.compare(baseline[:1], candidate[:1])["twofold_target_verified_for_this_fixture"]
assert not module.compare(baseline[:2], candidate)["twofold_target_verified_for_this_fixture"]
rejects(lambda: module.compare([], candidate), "empty baseline accepted")
rejects(lambda: module.compare(baseline, []), "empty candidate accepted")
rejects(lambda: module.compare(baseline, baseline), "same runs accepted twice")
for key, value in (("packages", ["kernel 1.0"]), ("packages", ["kernel 2.0", "shell 2.0"]),
                   ("explicit_packages", ["kernel", "shell"]), ("iso_sha256", "e" * 64),
                   ("test_overlay_sha256", None), ("direct_initrd_sha256", "4" * 64)):
  altered = copy.deepcopy(candidate)
  altered[0][key] = value
  rejects(lambda: module.compare(baseline, altered), f"incomparable {key} accepted")
for key, value in (("accelerator", "kvm"), ("disk_cache", "none"), ("encryption", True),
                   ("filesystem", "ext4"), ("cidata_configuration_sha256", "f" * 64)):
  altered = copy.deepcopy(candidate)
  # Entire groups may differ in image, but never in these fixture settings.
  for result in altered:
    result["fixture"][key] = value
  rejects(lambda: module.compare(baseline, altered), f"incomparable {key} accepted")
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
