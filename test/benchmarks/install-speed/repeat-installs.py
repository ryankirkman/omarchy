#!/usr/bin/env python3
"""Alternate fresh control/candidate installs, preserve verified evidence, reclaim stopped disks."""

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time


HERE = Path(__file__).resolve().parent
RUNNER = HERE.parent / "iso-vm.py"
COMPARATOR = HERE.parent / "compare-installs.py"
EVIDENCE_FILES = (
  "manifest.json", "install-timing.json", "validation.json", "identity.json",
  "package-manifest.txt", "package-explicit.txt", "package-files.txt", "package-files.stderr",
  "installed-root.json", "installed-boot.txt", "machine-id.txt", "ssh-host-fingerprints.txt",
  "pacman-master-keys.txt", "btrfs-uuid.txt", "btrfs-subvolumes.txt", "uki-files.txt",
  "serial.log", "live-serial.log", "qemu.log", "installed-screen.png",
  "systemd-analyze-blame.txt", "systemd-analyze-critical-chain.txt",
  "standalone-reboot.json", "standalone-root.json", "standalone-identity.json",
  "standalone-machine-id.txt", "standalone-ssh-host-fingerprints.txt", "standalone-pacman-master-keys.txt",
  "standalone-btrfs-uuid.txt", "standalone-btrfs-subvolumes.txt", "standalone-uki-files.txt",
)
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
FAILED_EVIDENCE_FILES = (*EVIDENCE_FILES,
  "progress.json", "latest-screen.png", "last-failed-ssh-probe.json", "timeout-diagnostics.json",
  "timeout-before-keys.png", "timeout-after-escape.png", "timeout-after-tty2.png",
)


def write_json(path, value):
  path.parent.mkdir(parents=True, exist_ok=True)
  temporary = path.with_name(path.name + ".tmp")
  temporary.write_text(json.dumps(value, indent=2) + "\n")
  temporary.replace(path)


def digest(path):
  with path.open("rb") as stream:
    return hashlib.file_digest(stream, "sha256").hexdigest()


def load_comparator():
  spec = importlib.util.spec_from_file_location("compare_installs", COMPARATOR)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def launch_template(path):
  argv = json.loads(path.read_text())
  if (not isinstance(argv, list) or len(argv) < 3 or not all(isinstance(item, str) for item in argv)
      or Path(argv[1]).resolve() != RUNNER or argv[2] != "run"):
    raise ValueError(f"{path}: expected full argv array for {RUNNER} run")
  result = [sys.executable, str(RUNNER), "run"]
  seen = set()
  index = 3
  while index < len(argv):
    item = argv[index]
    if item == "--keep-running":
      index += 1
      continue
    if item == "--run-dir":
      if index + 1 >= len(argv):
        raise ValueError("--run-dir has no value")
      index += 2
      continue
    if item.startswith("--"):
      if item in seen:
        raise ValueError(f"{path}: duplicate runner option {item}")
      seen.add(item)
    if item == "--mode" and (index + 1 >= len(argv) or argv[index + 1] != "install"):
      raise ValueError("Repeated benchmark samples must use install mode")
    result.append(item)
    index += 1
  if "--mode" not in seen:
    result.extend(["--mode", "install"])
  return result


def schedule(pairs, first):
  rows = []
  other = "candidate" if first == "control" else "control"
  for pair in range(1, pairs + 1):
    order = (first, other) if pair % 2 else (other, first)
    for revision in order:
      rows.append({"name": f"{len(rows) + 1:02d}-{revision}-pair{pair:02d}",
                   "revision": revision, "pair": pair})
  return rows


def active_runs(root, excluded=None):
  active = []
  for path in root.rglob("manifest.json") if root.exists() else []:
    if excluded and path.parent == excluded:
      continue
    try:
      manifest = json.loads(path.read_text())
    except (ValueError, OSError):
      continue
    if ("qemu_argv" in manifest and manifest.get("status") in {"running", "builder-ssh-ready"}
        and "qemu_exit_status" not in manifest):
      active.append(str(path.parent))
  return active


def verify_seal(directory):
  seal = json.loads((directory / "seal.json").read_text())
  for name, record in seal["files"].items():
    path = directory / name
    if Path(name).name != name or path.is_symlink() or not path.is_file():
      raise ValueError(f"Unsafe or missing sealed evidence file: {path}")
    if path.stat().st_size != record["bytes"] or digest(path) != record["sha256"]:
      raise ValueError(f"Sealed evidence changed: {path}")
  return seal


def seal_run(source, destination, comparator, provenance):
  # The real comparator validates completeness before any source disk can be
  # deleted. A staging directory prevents partial copies looking like success.
  comparator.read_run(source)
  manifest = json.loads((source / "manifest.json").read_text())
  if manifest.get("qemu_exit_status") != 0:
    raise ValueError("QEMU must exit cleanly before evidence sealing or disk reclamation")
  temporary = destination.with_name(destination.name + ".sealing")
  if destination.exists() or temporary.exists():
    raise ValueError(f"Refusing to overwrite existing evidence: {destination}")
  temporary.mkdir(parents=True)
  files = {}
  total = 0
  for name in EVIDENCE_FILES:
    original = source / name
    if not original.exists():
      continue
    if original.is_symlink() or not original.is_file():
      raise ValueError(f"Evidence must be a regular file: {original}")
    size = original.stat().st_size
    total += size
    if size > MAX_FILE_BYTES or total > MAX_EVIDENCE_BYTES:
      raise ValueError("Evidence exceeds small-artifact limits; retain source run for review")
    shutil.copyfile(original, temporary / name)
    files[name] = {"sha256": digest(original), "bytes": size}
    if digest(temporary / name) != files[name]["sha256"]:
      raise ValueError(f"Evidence copy differs: {name}")
  write_json(temporary / "seal.json", {
    "schema_version": 1, "sealed_at": time.time(), "source_run_directory": str(source),
    "qemu_exit_status": 0, "runner_exit_status": 0, "private_keys_and_virtual_disks_excluded": True,
    "files": files, **provenance,
  })
  comparator.read_run(temporary)
  verify_seal(temporary)
  temporary.rename(destination)
  for path in destination.iterdir():
    path.chmod(0o444)
  return verify_seal(destination)


def retain_failed_run(source, destination, error, runner_exit_status, provenance):
  # A failed comparator must not prevent CI from exporting the diagnostics.
  # This directory is never a valid seal, never added to series.runs, and never
  # permits disk reclamation. Original guest files keep their original contents.
  if destination.exists():
    raise ValueError(f"Refusing to overwrite failed-run evidence: {destination}")
  destination.mkdir(parents=True)
  record = {"schema_version": 1, "status": "failed", "measurement_valid": False,
            "failure": str(error), "runner_exit_status": runner_exit_status,
            "source_run_directory": str(source), "captured_at": time.time(),
            "private_keys_and_virtual_disks_excluded": True, "files": {},
            "capture_errors": {}, **provenance}
  write_json(destination / "failure-record.json", record)
  total = 0
  for name in FAILED_EVIDENCE_FILES:
    original = source / name
    if not original.exists():
      continue
    try:
      if original.is_symlink() or not original.is_file():
        raise ValueError("Evidence must be a regular file")
      size = original.stat().st_size
      if size > MAX_FILE_BYTES or total + size > MAX_EVIDENCE_BYTES:
        raise ValueError("Evidence exceeds small-artifact limits")
      expected = digest(original)
      shutil.copyfile(original, destination / name)
      total += size
      if digest(destination / name) != expected:
        raise ValueError("Evidence changed during capture")
      record["files"][name] = {"sha256": expected, "bytes": size}
    except (OSError, ValueError) as capture_error:
      record["capture_errors"][name] = str(capture_error)
    write_json(destination / "failure-record.json", record)
  return record


def reclaim_target(run, evidence, runner_returncode):
  # No recursive cleanup: only this exact freshly created target is eligible.
  # Never delete firmware, a source image, an active disk, or a failed run.
  manifest = json.loads((run / "manifest.json").read_text())
  if runner_returncode != 0 or manifest.get("qemu_exit_status") != 0 or manifest.get("status") != "installed-and-booted":
    raise ValueError("Target reclamation requires successful validation and both processes exited cleanly")
  verify_seal(evidence)
  disk = run / "target.qcow2"
  if disk.is_symlink() or disk.resolve().parent != run.resolve():
    raise ValueError("Target disk is not this run's own regular path")
  allocated = disk.stat().st_blocks * 512 if disk.exists() else 0
  if disk.exists():
    disk.unlink()
  return {"target": str(disk), "reclaimed_allocated_bytes": allocated, "at": time.time()}


def execute_sample(argv, run, log_path, shutdown_timeout):
  # stdout/stderr stay in unsynced scratch; progress is also emitted by iso-vm.
  with log_path.open("w") as log:
    child = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    validated_at = None
    next_report = 0
    try:
      while child.poll() is None:
        now = time.monotonic()
        manifest_path = run / "manifest.json"
        if manifest_path.exists():
          manifest = json.loads(manifest_path.read_text())
          if manifest.get("status") == "installed-and-booted" and validated_at is None:
            validated_at = now
          if validated_at is not None and now - validated_at > shutdown_timeout:
            raise TimeoutError("Validated guest failed to shut down; retaining its target disk")
        if now >= next_report:
          progress = run / "progress.json"
          details = json.loads(progress.read_text()) if progress.exists() else {}
          print(json.dumps({**details, "event": "sample-progress", "run": run.name}), flush=True)
          next_report = now + 30
        time.sleep(1)
      return child.returncode
    except BaseException:
      # Request ordinary poweroff through the supervisor. If it cannot finish,
      # terminate the supervisor; no disk is reclaimed on this failure path.
      request = run / "requests" / "repeat-driver-stop.json"
      if request.parent.exists():
        write_json(request, {"action": "qmp", "execute": "system_powerdown"})
      try:
        child.wait(timeout=30)
      except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGTERM)
        try:
          child.wait(timeout=10)
        except subprocess.TimeoutExpired:
          os.killpg(child.pid, signal.SIGKILL)
          child.wait()
      raise


class DriverParser(argparse.ArgumentParser):
  def error(self, message):
    # Exit 2 is reserved for a complete valid experiment below the goal.
    self.print_usage(sys.stderr)
    raise ValueError(message)


def main():
  parser = DriverParser(description=__doc__)
  parser.add_argument("--control-launch", type=Path, required=True)
  parser.add_argument("--candidate-launch", type=Path, required=True)
  parser.add_argument("--run-root", type=Path, required=True, help="Unsynced scratch for mutable VM state")
  parser.add_argument("--evidence-root", type=Path, required=True, help="Small sealed artifacts, safe to retain in git")
  parser.add_argument("--vm-state-root", type=Path, help="Scan existing VM manifests here; defaults to run-root parent")
  parser.add_argument("--pairs", type=int, default=3)
  parser.add_argument("--first", choices=("control", "candidate"), default="control")
  parser.add_argument("--shutdown-timeout", type=int, default=180)
  parser.add_argument("--resume", action="store_true")
  parser.add_argument("--plan-only", action="store_true")
  args = parser.parse_args()
  def interrupted(signum, _frame):
    raise KeyboardInterrupt(f"signal {signum}")
  signal.signal(signal.SIGTERM, interrupted)
  signal.signal(signal.SIGINT, interrupted)
  if args.pairs < 3:
    parser.error("At least three fresh samples of each revision are required")
  args.run_root = args.run_root.resolve()
  args.evidence_root = args.evidence_root.resolve()
  args.vm_state_root = (args.vm_state_root or args.run_root.parent).resolve()
  if args.run_root == args.evidence_root or args.evidence_root.is_relative_to(args.run_root):
    parser.error("Keep sealed evidence outside mutable run-root")
  templates = {"control": launch_template(args.control_launch), "candidate": launch_template(args.candidate_launch)}
  rows = schedule(args.pairs, args.first)
  provenance = {"runner_sha256": digest(RUNNER), "comparator_sha256": digest(COMPARATOR),
                "repeat_driver_sha256": digest(Path(__file__))}
  plan = {"schema_version": 1, "pairs": args.pairs, "first": args.first,
          "run_root": str(args.run_root), "evidence_root": str(args.evidence_root),
          "templates": templates, "order": rows, "source_provenance": provenance}
  if args.plan_only:
    print(json.dumps(plan, indent=2))
    return 0
  args.vm_state_root.mkdir(parents=True, exist_ok=True)
  lock = (args.vm_state_root / ".repeat-installs.lock").open("a")
  try:
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
  except BlockingIOError:
    parser.error("Another repeated-install driver owns this VM state root")
  args.run_root.mkdir(parents=True, exist_ok=True)
  args.evidence_root.mkdir(parents=True, exist_ok=True)
  series_path = args.evidence_root / "series.json"
  if args.resume:
    series = json.loads(series_path.read_text())
    if series["plan"] != plan:
      raise ValueError("Resume inputs/order/source code differ; use a fresh series")
  else:
    if series_path.exists() or any(args.evidence_root.iterdir()):
      raise ValueError("Evidence root already contains work; use --resume or a new directory")
    series = {"plan": plan, "started_at": time.time(), "status": "running", "runs": []}
    write_json(series_path, series)
  comparator = load_comparator()
  completed = {row["name"]: row for row in series["runs"]}
  current_run = None
  current_result = None
  try:
    for row in rows:
      name = row["name"]
      run = args.run_root / name
      current_run, current_result = run, None
      evidence = args.evidence_root / "runs" / name
      if name in completed:
        verify_seal(evidence)
        comparator.read_run(evidence)
        continue
      active = active_runs(args.vm_state_root)
      if active:
        raise RuntimeError("Existing VM has no recorded shutdown: " + ", ".join(active))
      if any(digest(path) != provenance[key] for key, path in (
        ("runner_sha256", RUNNER), ("comparator_sha256", COMPARATOR), ("repeat_driver_sha256", Path(__file__)),
      )):
        raise RuntimeError("Benchmark source changed during the series")
      if evidence.exists():
        # Recover a crash between sealing and series checkpoint/reclamation.
        seal = verify_seal(evidence)
        if any(seal.get(key) != value for key, value in provenance.items()):
          raise ValueError("Sealed sample source provenance differs from this series")
        if seal.get("runner_exit_status") != 0 or seal.get("source_run_directory") != str(run):
          raise ValueError("Sealed sample does not prove this run exited successfully")
        comparator.read_run(evidence)
        result = 0
      else:
        if run.exists():
          raise RuntimeError(f"Unsealed existing run retained for investigation: {run}")
        argv = [*templates[row["revision"]], "--run-dir", str(run)]
        write_json(args.run_root / (name + "-launch.json"), argv)
        print(json.dumps({"event": "sample-started", **row, "run_dir": str(run)}), flush=True)
        result = execute_sample(argv, run, args.run_root / (name + "-supervisor.log"), args.shutdown_timeout)
        current_result = result
        if result != 0:
          raise RuntimeError(f"Runner failed with exit {result}; evidence/disk retained at {run}")
        seal_run(run, evidence, comparator, provenance)
      current_result = result
      # Cross-run package, identity, media and fixture checks run before freeing
      # the newest disk as soon as both revisions have one sample.
      trial = [*series["runs"], {**row, "evidence": str(evidence)}]
      groups = {revision: [comparator.read_run(Path(item["evidence"])) for item in trial if item["revision"] == revision]
                for revision in ("control", "candidate")}
      if all(groups.values()):
        comparison = comparator.compare(groups["control"], groups["candidate"])
        if not comparison["host_boot_to_installed_ssh"]["comparable"]:
          raise RuntimeError("Control/candidate boot fixtures differ; refusing an unmatched whole-install series")
        write_json(args.evidence_root / "comparison.json", comparison)
      reclaim = reclaim_target(run, evidence, result)
      finished = {**row, "evidence": str(evidence), "runner_exit_status": result, "reclamation": reclaim}
      series["runs"].append(finished)
      write_json(series_path, series)
      print(json.dumps({"event": "sample-sealed", **finished}), flush=True)
    groups = {revision: [comparator.read_run(Path(item["evidence"])) for item in series["runs"] if item["revision"] == revision]
              for revision in ("control", "candidate")}
    comparison = comparator.compare(groups["control"], groups["candidate"])
    write_json(args.evidence_root / "comparison.json", comparison)
    achieved = comparison["twofold_target_verified_for_this_fixture"]
    series.update(status="complete-target-verified" if achieved else "complete-target-not-met", finished_at=time.time())
    write_json(series_path, series)
    print(json.dumps({"event": series["status"], "comparison": str(args.evidence_root / "comparison.json")}), flush=True)
    return 0 if achieved else 2
  except BaseException as error:
    series.update(status="failed", failure=str(error), failed_at=time.time())
    if current_run is not None and current_run.is_dir():
      destination = args.evidence_root / "failed-runs" / current_run.name
      try:
        retain_failed_run(current_run, destination, error, current_result, provenance)
        series["failed_run_evidence"] = str(destination)
      except (OSError, ValueError) as capture_error:
        # The original installation/comparison error remains authoritative.
        series["failed_run_capture_error"] = str(capture_error)
    write_json(series_path, series)
    raise


if __name__ == "__main__":
  try:
    raise SystemExit(main())
  except KeyboardInterrupt:
    raise SystemExit(130)
  except (OSError, ValueError, RuntimeError, KeyError) as error:
    print(f"repeat-installs: {error}", file=sys.stderr)
    raise SystemExit(1)
