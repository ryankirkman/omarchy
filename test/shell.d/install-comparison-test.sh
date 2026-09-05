#!/bin/bash

set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/base-test.sh"

python3 - "$ROOT" <<'PY'
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

with tempfile.TemporaryDirectory() as directory:
  root = Path(directory)
  manifest = {
    "status": "installed-and-booted", "fresh_target": True, "fresh_nvram": True,
    "accelerator": "tcg", "cpu_count": 4, "memory_mib": 8192,
    "disk_format": "qcow2", "disk_virtual_bytes": 40 * 1024**3,
    "disk_cache": "writeback", "iso_cache": "writeback", "qemu_version": "fixture",
    "iso_sha256": "fixture-only",
  }
  timing = {"current_phase": "Installation complete", "total_phases": 1,
            "phases": [{"name": "Install", "status": "ok", "elapsed": 12}],
            "installed_packages": 2, "started_at": 100, "finished_at": 112}
  validation = {"booted_installed_root": True, "package_files_exit_status": 0}
  for name, value in (("manifest.json", manifest), ("install-timing.json", timing), ("validation.json", validation)):
    (root / name).write_text(json.dumps(value))
  (root / "package-manifest.txt").write_text("kernel 1.0\nshell 2.0\n")
  run = module.read_run(root)
  assert run["elapsed"] == 12
  for key, value in (("booted_installed_root", False), ("package_files_exit_status", 1)):
    (root / "validation.json").write_text(json.dumps({**validation, key: value}))
    rejects(lambda: module.read_run(root), "invalid installation accepted")
  (root / "validation.json").write_text(json.dumps(validation))
  (root / "package-manifest.txt").write_text("kernel 1.0\nkernel 2.0\n")
  rejects(lambda: module.read_run(root), "duplicate packages accepted")

def sample(name, seconds):
  result = copy.deepcopy(run)
  result.update(directory=name, elapsed=seconds)
  return result

baseline = [sample(f"baseline-{index}", 12) for index in range(3)]
candidate = [sample(f"candidate-{index}", 5) for index in range(3)]
assert module.compare(baseline, candidate)["twofold_target_verified_for_this_fixture"]
assert not module.compare(baseline[:1], candidate[:1])["twofold_target_verified_for_this_fixture"]
rejects(lambda: module.compare(baseline, baseline), "same runs accepted twice")
altered = copy.deepcopy(candidate)
altered[0]["packages"] = ["kernel 1.0"]
rejects(lambda: module.compare(baseline, altered), "skipped packages accepted")
altered = copy.deepcopy(candidate)
altered[0]["fixture"]["accelerator"] = "kvm"
rejects(lambda: module.compare(baseline, altered), "incomparable accelerators accepted")
candidate[-1]["elapsed"] = 7
assert not module.compare(baseline, candidate)["twofold_target_verified_for_this_fixture"]
PY

pass "install comparison rejects failed, incomplete, repeated, or incomparable results"
