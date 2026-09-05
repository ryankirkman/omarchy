#!/usr/bin/env python3
"""Compare validated fresh VM installs; never promote component results to installs."""

import argparse
import base64
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import uuid


MEDIA_CACHE_PRECONDITIONING = "sha256-read-iso-then-extra-media-in-array-order-then-kernel-then-initrd-before-vm-start"


def sha256(value, description):
  if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
    raise ValueError(f"{description}: expected a SHA256 digest")
  return value.lower()


def finite_number(value):
  return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def identity_evidence(directory):
  identity = json.loads((directory / "identity.json").read_text())
  if not isinstance(identity, dict):
    raise ValueError(f"{directory}: machine identity evidence must be an object")
  machine = identity["machine_id"]
  if not isinstance(machine, str) or not re.fullmatch(r"[0-9a-f]{32}", machine) or int(machine, 16) == 0:
    raise ValueError(f"{directory}: missing or invalid machine ID")
  master = identity["pacman_master_key_fingerprint"]
  if (not isinstance(master, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", master)
      or int(master, 16) == 0):
    raise ValueError(f"{directory}: missing or invalid local pacman master public-key fingerprint")
  filesystem = identity["btrfs_uuid"]
  if not isinstance(filesystem, str) or not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", filesystem):
    raise ValueError(f"{directory}: missing or invalid Btrfs filesystem UUID")
  filesystem = uuid.UUID(filesystem)
  if filesystem.int == 0:
    raise ValueError(f"{directory}: Btrfs filesystem UUID is empty")
  keys = identity["ssh_host_key_fingerprints"]
  if not isinstance(keys, list) or not keys:
    raise ValueError(f"{directory}: SSH host public-key fingerprints are missing")
  for key in keys:
    if not isinstance(key, str) or not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", key):
      raise ValueError(f"{directory}: invalid SSH host public-key fingerprint")
    digest = base64.b64decode(key[7:] + "=", validate=True)
    if not any(digest) or base64.b64encode(digest).decode().rstrip("=") != key[7:]:
      raise ValueError(f"{directory}: empty or noncanonical SSH host public-key fingerprint")
  if len(set(keys)) != len(keys):
    raise ValueError(f"{directory}: duplicate SSH host public-key fingerprints")
  # Only public identifiers enter reports. The pacman fingerprint identifies
  # this installation's local primary signing key, not imported distro keys.
  return {"machine_id": machine, "pacman_master_key_fingerprint": master.upper(),
          "btrfs_uuid": str(filesystem), "ssh_host_key_fingerprints": sorted(keys)}


def media_evidence(manifest, directory):
  if manifest["media_cache_preconditioning"] != MEDIA_CACHE_PRECONDITIONING:
    raise ValueError(f"{directory}: unsupported or missing media cache preconditioning policy")
  media = manifest["extra_media"]
  if not isinstance(media, list):
    raise ValueError(f"{directory}: supplementary media must be an ordered list")
  recorded = []
  identifiers = set()
  for medium in media:
    if not isinstance(medium, dict) or medium.get("readonly") is not True:
      raise ValueError(f"{directory}: supplementary install media must be read-only")
    path = medium["path"]
    if not isinstance(path, str) or not Path(path).is_absolute():
      raise ValueError(f"{directory}: supplementary media requires an absolute source path")
    topology = {key: medium[key] for key in ("drive_id", "format", "cache", "interface", "device", "readonly")}
    for key in ("format", "cache", "interface"):
      if not isinstance(topology[key], str) or not topology[key]:
        raise ValueError(f"{directory}: supplementary media {key} is missing")
    for key in ("drive_id", "device"):
      if topology[key] is not None and (not isinstance(topology[key], str) or not topology[key]):
        raise ValueError(f"{directory}: invalid supplementary media {key}")
    identifier = topology["drive_id"]
    if identifier is not None:
      if identifier in identifiers:
        raise ValueError(f"{directory}: duplicate supplementary media drive ID")
      identifiers.add(identifier)
    recorded.append({"path": path, "sha256": sha256(medium["sha256"], f"{directory}: supplementary media"), **topology})
  return recorded


def boot_evidence(manifest, directory):
  upper = manifest["first_installed_ssh_wall_s"]
  lower = manifest["last_failed_installed_ssh_probe_started_wall_s"]
  uncertainty = manifest["readiness_poll_uncertainty_s"]
  if (not all(finite_number(value) for value in (lower, upper, uncertainty))
      or upper <= 0 or lower < 0 or lower > upper or uncertainty < 0
      or not math.isclose(uncertainty, upper - lower, rel_tol=1e-9, abs_tol=1e-6)):
    raise ValueError(f"{directory}: invalid actual SSH readiness bracket")
  # A failed probe may return after readiness changes, so its start is the
  # conservative lower bound. Nominal polling frequency is not an error bound.
  if "last_failed_installed_ssh_wall_s" in manifest:
    failed_end = manifest["last_failed_installed_ssh_wall_s"]
    if not finite_number(failed_end) or not lower <= failed_end <= upper:
      raise ValueError(f"{directory}: invalid failed SSH probe chronology")
  direct = manifest["direct_kernel_boot"]
  if type(direct) is not bool:
    raise ValueError(f"{directory}: direct kernel boot must be recorded explicitly")
  kernel = manifest["direct_kernel_sha256"]
  initrd = manifest["direct_initrd_sha256"]
  command_line = manifest["direct_kernel_command_line"]
  if direct:
    kernel = sha256(kernel, f"{directory}: direct boot kernel")
    initrd = sha256(initrd, f"{directory}: direct boot initrd")
    if not isinstance(command_line, str) or not command_line:
      raise ValueError(f"{directory}: direct boot kernel command line is missing")
  elif any(value is not None for value in (kernel, initrd, command_line)):
    raise ValueError(f"{directory}: firmware boot unexpectedly records direct boot inputs")
  strategy = manifest["reboot_strategy"]
  if not isinstance(strategy, str) or not strategy:
    raise ValueError(f"{directory}: reboot strategy is missing")
  return {
    "boot_fixture": {"direct_kernel_boot": direct, "direct_kernel_sha256": kernel,
                     "direct_kernel_command_line": command_line, "reboot_strategy": strategy},
    "direct_initrd_sha256": initrd,
    "boot_to_ssh_seconds": upper, "ssh_readiness_lower_bound_seconds": lower,
    "ssh_poll_uncertainty_seconds": uncertainty,
  }


def read_run(directory):
  directory = Path(directory)
  manifest = json.loads((directory / "manifest.json").read_text())
  timing = json.loads((directory / "install-timing.json").read_text())
  validation = json.loads((directory / "validation.json").read_text())
  packages = (directory / "package-manifest.txt").read_text().splitlines()
  explicit_packages = (directory / "package-explicit.txt").read_text().splitlines()
  if manifest.get("status") != "installed-and-booted":
    raise ValueError(f"{directory}: install has not booted successfully")
  if manifest.get("mode") != "install":
    raise ValueError(f"{directory}: builder or unrecorded run mode is not an installation sample")
  if manifest.get("measurement_interrupted") is not False:
    raise ValueError(f"{directory}: uninterrupted measurement was not recorded")
  if validation.get("booted_installed_root") is not True:
    raise ValueError(f"{directory}: installed root was not independently verified")
  if type(validation.get("package_files_exit_status")) is not int or validation["package_files_exit_status"] != 0:
    raise ValueError(f"{directory}: package file validation failed")
  phases = timing.get("phases", [])
  if (timing.get("current_phase") != "Installation complete" or not phases
      or len(phases) != timing.get("total_phases")
      or any(phase.get("status") != "ok" for phase in phases)):
    raise ValueError(f"{directory}: missing or failed installer phases")
  if not packages or any(len(line.split()) != 2 for line in packages):
    raise ValueError(f"{directory}: empty or malformed package manifest")
  package_names = {line.split()[0] for line in packages}
  if len(package_names) != len(packages):
    raise ValueError(f"{directory}: duplicate package names")
  if (any(len(line.split()) != 1 or line != line.strip() for line in explicit_packages)
      or len(set(explicit_packages)) != len(explicit_packages)
      or not set(explicit_packages).issubset(package_names)):
    raise ValueError(f"{directory}: malformed or inconsistent explicit package inventory")
  if len(packages) != timing.get("installed_packages"):
    raise ValueError(f"{directory}: package inventory disagrees with installer count")
  if not all(finite_number(timing[key]) for key in ("started_at", "finished_at")):
    raise ValueError(f"{directory}: invalid installer timestamps")
  elapsed = timing["finished_at"] - timing["started_at"]
  if not math.isfinite(elapsed) or elapsed <= 0:
    raise ValueError(f"{directory}: invalid installer elapsed time")
  names = set()
  for phase in phases:
    if not finite_number(phase.get("elapsed")) or phase["elapsed"] < 0:
      raise ValueError(f"{directory}: invalid phase timing")
    name = phase.get("name")
    if not isinstance(name, str) or not name or name in names:
      raise ValueError(f"{directory}: missing or duplicate installer phase name")
    names.add(name)
  fixture = {key: manifest[key] for key in ("accelerator", "cpu_count", "memory_mib")}
  if manifest.get("fresh_target") is not True or manifest.get("fresh_nvram") is not True:
    raise ValueError(f"{directory}: fresh disk and NVRAM are not recorded")
  for key in ("disk_format", "disk_virtual_bytes", "disk_cache", "iso_cache", "qemu_version", "encryption", "filesystem"):
    fixture[key] = manifest[key]
    if fixture[key] is None or fixture[key] == "":
      raise ValueError(f"{directory}: missing fixture setting {key}")
  fixture["cidata_configuration_sha256"] = sha256(manifest["cidata_configuration_sha256"], f"{directory}: cidata configuration")
  extra_media = media_evidence(manifest, directory)
  fixture["extra_media_topology"] = [{key: value for key, value in medium.items() if key not in {"path", "sha256"}} for medium in extra_media]
  fixture["media_cache_preconditioning"] = manifest["media_cache_preconditioning"]
  overlay = manifest["test_overlay_sha256"]
  if overlay is not None:
    overlay = sha256(overlay, f"{directory}: test overlay")
  return {
    "directory": str(directory.resolve()), "fixture": fixture,
    "packages": sorted(" ".join(line.split()) for line in packages),
    "explicit_packages": sorted(explicit_packages), "elapsed": elapsed,
    "phase_seconds": {phase["name"]: phase["elapsed"] for phase in phases},
    "identity": identity_evidence(directory),
    **boot_evidence(manifest, directory),
    "iso_sha256": sha256(manifest["iso_sha256"], f"{directory}: ISO"),
    "test_overlay_sha256": overlay,
    "extra_media": extra_media,
  }


def compare(baseline, candidate):
  if not baseline or not candidate:
    raise ValueError("both revisions require at least one validated installation")
  all_runs = baseline + candidate
  if len({run["directory"] for run in all_runs}) != len(all_runs):
    raise ValueError("each sample must be a distinct fresh installation")
  for key in ("machine_id", "pacman_master_key_fingerprint", "btrfs_uuid"):
    if len({run["identity"][key] for run in all_runs}) != len(all_runs):
      raise ValueError(f"fresh installations share {key}; cloned machine identity is not acceptable")
  host_keys = [key for run in all_runs for key in run["identity"]["ssh_host_key_fingerprints"]]
  if len(set(host_keys)) != len(host_keys):
    raise ValueError("fresh installations share an SSH host public-key fingerprint")
  for run in all_runs:
    if run["fixture"] != baseline[0]["fixture"]:
      raise ValueError("hardware, VM I/O, media topology/cache policy, encryption, filesystem or cidata configuration differ between runs")
    if run["packages"] != baseline[0]["packages"]:
      raise ValueError("installed package names or versions differ between runs")
    if run["explicit_packages"] != baseline[0]["explicit_packages"]:
      raise ValueError("installed package explicit/dependency reasons differ between runs")
  for group in (baseline, candidate):
    # A candidate is allowed to change its ISO or overlay. Repetitions of each
    # revision must still measure the same input, never a mixture of candidates.
    if len({(run["iso_sha256"], run["test_overlay_sha256"], run["direct_initrd_sha256"],
             tuple(medium["sha256"] for medium in run["extra_media"])) for run in group}) != 1:
      raise ValueError("a revision group contains multiple ISO images, test overlays, initrds or supplementary media")
  durations = {"baseline": [run["elapsed"] for run in baseline],
               "candidate": [run["elapsed"] for run in candidate]}
  medians = {key: statistics.median(values) for key, values in durations.items()}
  conservative = min(durations["baseline"]) / max(durations["candidate"])
  boot_comparable = all(run["boot_fixture"] == baseline[0]["boot_fixture"] for run in all_runs)
  boot_upper = {"baseline": [run["boot_to_ssh_seconds"] for run in baseline],
                "candidate": [run["boot_to_ssh_seconds"] for run in candidate]}
  boot_lower = {"baseline": [run["ssh_readiness_lower_bound_seconds"] for run in baseline],
                "candidate": [run["ssh_readiness_lower_bound_seconds"] for run in candidate]}
  boot_medians = {key: statistics.median(values) for key, values in boot_upper.items()}
  boot_conservative = min(boot_lower["baseline"]) / max(boot_upper["candidate"]) if boot_comparable else None
  repeated = len(baseline) >= 3 and len(candidate) >= 3
  return {
    "schema_version": 5, "kind": "validated_full_install_comparison",
    "fixture": baseline[0]["fixture"],
    "scope": "Host VM start through live boot, installation, reboot and first successful SSH to the independently verified installed root. Package files are checked afterward.",
    "clock": "Host monotonic clock across any QEMU restart, with actual SSH probe uncertainty. Guest installer wall clock is a separate component metric.",
    "package_count": len(baseline[0]["packages"]),
    "package_manifest_sha256": hashlib.sha256(("\n".join(baseline[0]["packages"]) + "\n").encode()).hexdigest(),
    "explicit_package_count": len(baseline[0]["explicit_packages"]),
    "explicit_package_manifest_sha256": hashlib.sha256("".join(name + "\n" for name in baseline[0]["explicit_packages"]).encode()).hexdigest(),
    "distinct_installation_identities_verified": True,
    "guest_installer": {
      "seconds": durations, "median_seconds": medians,
      "median_speedup": medians["baseline"] / medians["candidate"],
      "fastest_baseline_over_slowest_candidate": conservative,
      "twofold_verified_for_this_fixture": repeated and conservative >= 2,
      "scope": "Guest installer phases only; excludes work performed during live boot or after phase completion.",
    },
    "host_boot_to_installed_ssh": {
      "comparable": boot_comparable,
      "incomparable_reason": None if boot_comparable else "Direct kernel boot, kernel digest, kernel command line or reboot strategy differs between runs.",
      "readiness_upper_bound_seconds": boot_upper, "readiness_lower_bound_seconds": boot_lower,
      "median_observed_seconds": boot_medians,
      "median_observed_speedup": boot_medians["baseline"] / boot_medians["candidate"] if boot_comparable else None,
      "conservative_speedup_lower_bound": boot_conservative,
      "bound": "Fastest baseline readiness lower bound divided by slowest candidate readiness upper bound; includes actual polling uncertainty.",
    },
    "at_least_three_fresh_samples_per_revision": repeated,
    "twofold_target_verified_for_this_fixture": repeated and boot_comparable and boot_conservative >= 2,
    "limitations": "No generalization across hardware, media, encryption modes or thermal states. Software-emulation results do not establish physical-machine speedups.",
    "runs": [{key: value for key, value in run.items() if key not in {"packages", "explicit_packages"}} for run in all_runs],
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
