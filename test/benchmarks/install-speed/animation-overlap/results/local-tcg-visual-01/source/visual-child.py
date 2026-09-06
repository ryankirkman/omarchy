#!/usr/bin/env python3
"""Harmless installer-shaped child for the disposable live console fixture."""

import argparse
import json
from pathlib import Path
import time


def save(path, data):
  temporary = path.with_name(path.name + ".tmp")
  temporary.write_text(json.dumps(data, indent=2) + "\n")
  temporary.replace(path)


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--directory", type=Path, required=True)
parser.add_argument("--mode", choices=("success", "failure", "fallback"), required=True)
args = parser.parse_args()
directory = args.directory.resolve()
if not directory.is_relative_to(Path("/tmp/omarchy-animation-visual")):
  raise SystemExit("Fixture child requires its disposable /tmp namespace")
started = time.monotonic()
state = {"started_at": time.time(), "target": str(directory / "unused-target"), "total_phases": 9,
  "current_index": 2, "current_phase": "Installing Arch + Omarchy", "phases": []}
save(directory / "state.json", state)
events = [{"event": "child-started", "monotonic_s": started}]
time.sleep(1)
state.update(current_index=6, current_phase=("Configuring system" if args.mode == "fallback"
  else "Finalizing boot and user setup"), phase_started_at=time.time())
save(directory / "state.json", state)
events.append({"event": "finalization-window", "monotonic_s": time.monotonic()})
time.sleep(2 if args.mode == "failure" else 12)
status = 17 if args.mode == "failure" else 0
if status:
  state.update(current_phase="Finalizing boot and user setup", phases=[{
    "name": "Finalizing boot and user setup", "status": "failed", "error": "Scripted visual-fixture failure"}])
else:
  state.update(current_phase="Installation complete", finished_at=time.time(),
    duration_seconds=time.monotonic() - started, installed_packages=941)
save(directory / "state.json", state)
events.append({"event": "child-exited", "monotonic_s": time.monotonic(), "exit_status": status})
save(directory / "child-events.json", events)
print("Scripted visual fixture complete; no installation was performed.", flush=True)
raise SystemExit(status)
