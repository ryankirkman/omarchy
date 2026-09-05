#!/usr/bin/env python3
"""Compare validated fresh VM installs; never promote component results to installs."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics


def read_run(directory):
  directory = Path(directory)
  manifest = json.loads((directory / "manifest.json").read_text())
  timing = json.loads((directory / "install-timing.json").read_text())
  validation = json.loads((directory / "validation.json").read_text())
  packages = (directory / "package-manifest.txt").read_text().splitlines()
  if manifest.get("status") != "installed-and-booted":
    raise ValueError(f"{directory}: install has not booted successfully")
  if validation.get("booted_installed_root") is not True:
    raise ValueError(f"{directory}: installed root was not independently verified")
  if validation.get("package_files_exit_status") != 0:
    raise ValueError(f"{directory}: package file validation failed")
  phases = timing.get("phases", [])
  if (timing.get("current_phase") != "Installation complete" or not phases
      or len(phases) != timing.get("total_phases")
      or any(phase.get("status") != "ok" for phase in phases)):
    raise ValueError(f"{directory}: missing or failed installer phases")
  if not packages or any(len(line.split()) != 2 for line in packages):
    raise ValueError(f"{directory}: empty or malformed package manifest")
  if len({line.split()[0] for line in packages}) != len(packages):
    raise ValueError(f"{directory}: duplicate package names")
  if len(packages) != timing.get("installed_packages"):
    raise ValueError(f"{directory}: package inventory disagrees with installer count")
  elapsed = timing["finished_at"] - timing["started_at"]
  if not math.isfinite(elapsed) or elapsed <= 0:
    raise ValueError(f"{directory}: invalid installer elapsed time")
  for phase in phases:
    if not math.isfinite(phase.get("elapsed", -1)) or phase["elapsed"] < 0:
      raise ValueError(f"{directory}: invalid phase timing")
  fixture = {key: manifest[key] for key in ("accelerator", "cpu_count", "memory_mib")}
  # QEMU options include host paths and ports, so retain them in source artifacts
  # and require the caller to inspect disk/cache/media settings before a claim.
  if manifest.get("fresh_target") is not True or manifest.get("fresh_nvram") is not True:
    raise ValueError(f"{directory}: fresh disk and NVRAM are not recorded")
  for key in ("disk_format", "disk_virtual_bytes", "disk_cache", "iso_cache", "qemu_version"):
    fixture[key] = manifest[key]
  return {
    "directory": str(directory.resolve()), "fixture": fixture,
    "packages": sorted(packages), "elapsed": elapsed,
    "phase_seconds": {phase["name"]: phase["elapsed"] for phase in phases},
    "boot_to_ssh_seconds": manifest.get("first_installed_ssh_wall_s"),
    "ssh_poll_uncertainty_seconds": manifest.get("readiness_poll_interval_s"),
    "iso_sha256": manifest["iso_sha256"],
  }


def compare(baseline, candidate):
  all_runs = baseline + candidate
  if len({run["directory"] for run in all_runs}) != len(all_runs):
    raise ValueError("each sample must be a distinct fresh installation")
  for run in all_runs:
    if run["fixture"] != baseline[0]["fixture"]:
      raise ValueError("hardware or VM I/O settings differ between runs")
    if run["packages"] != baseline[0]["packages"]:
      raise ValueError("installed package names or versions differ between runs")
  for group in (baseline, candidate):
    if len({run["iso_sha256"] for run in group}) != 1:
      raise ValueError("a revision group contains multiple ISO images")
  durations = {"baseline": [run["elapsed"] for run in baseline],
               "candidate": [run["elapsed"] for run in candidate]}
  medians = {key: statistics.median(values) for key, values in durations.items()}
  conservative = min(durations["baseline"]) / max(durations["candidate"])
  repeated = len(baseline) >= 3 and len(candidate) >= 3
  return {
    "schema_version": 1, "kind": "validated_full_install_comparison",
    "fixture": baseline[0]["fixture"],
    "scope": "Guest installer start through completion, then independently verified installed boot and package files.",
    "clock": "Existing guest installer wall clock; boot-to-SSH uses host monotonic clock separately.",
    "package_count": len(baseline[0]["packages"]),
    "package_manifest_sha256": hashlib.sha256(("\n".join(baseline[0]["packages"]) + "\n").encode()).hexdigest(),
    "installer_seconds": durations, "median_seconds": medians,
    "median_speedup": medians["baseline"] / medians["candidate"],
    "fastest_baseline_over_slowest_candidate": conservative,
    "at_least_three_fresh_samples_per_revision": repeated,
    "twofold_target_verified_for_this_fixture": repeated and conservative >= 2,
    "limitations": "No generalization across hardware, media, encryption modes or thermal states. Software-emulation results do not establish physical-machine speedups.",
    "runs": [{key: value for key, value in run.items() if key != "packages"} for run in all_runs],
  }


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--baseline", nargs="+", type=Path, required=True)
  parser.add_argument("--candidate", nargs="+", type=Path, required=True)
  parser.add_argument("--output", type=Path, required=True)
  args = parser.parse_args()
  try:
    result = compare([read_run(path) for path in args.baseline], [read_run(path) for path in args.candidate])
  except (OSError, ValueError, KeyError, TypeError) as error:
    parser.error(str(error))
  args.output.parent.mkdir(parents=True, exist_ok=True)
  args.output.write_text(json.dumps(result, indent=2) + "\n")
  print(json.dumps(result, indent=2))


if __name__ == "__main__":
  main()
