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
  for key, value in (("booted_installed_root", False), ("package_files_exit_status", 1),
                     ("package_files_exit_status", False)):
    rejects_artifacts({"validation.json": {**validation, key: value}}, "invalid installation accepted")
  for key, value in (("status", "timeout"), ("measurement_interrupted", True),
                     ("measurement_interrupted", 0), ("fresh_target", False), ("fresh_nvram", False),
                     ("cidata_configuration_sha256", "unknown"), ("test_overlay_sha256", "unknown"),
                     ("iso_sha256", "unknown")):
    rejects_artifacts({"manifest.json": {**manifest, key: value}}, f"invalid {key} accepted")
  for key in ("measurement_interrupted", "encryption", "filesystem", "cidata_configuration_sha256", "test_overlay_sha256"):
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


def sample(name, seconds):
  result = copy.deepcopy(run)
  result.update(directory=name, elapsed=seconds)
  return result


baseline = [sample(f"baseline-{index}", 12) for index in range(3)]
candidate = [sample(f"candidate-{index}", 5) for index in range(3)]
# Different candidate inputs are expected; each group still needs one revision.
for result in candidate:
  result.update(iso_sha256="c" * 64, test_overlay_sha256="d" * 64)
comparison = module.compare(baseline, candidate)
assert comparison["twofold_target_verified_for_this_fixture"]
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
                   ("test_overlay_sha256", None)):
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
candidate[-1]["elapsed"] = 7
comparison = module.compare(baseline, candidate)
assert comparison["median_speedup"] > 2
assert not comparison["twofold_target_verified_for_this_fixture"]
PYTEST

pass "install comparison rejects interrupted, incomplete, repeated, or incomparable results"
