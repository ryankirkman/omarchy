#!/usr/bin/env python3
"""Run exact dashboards and real terminal effects against harmless live children."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time


ROOT = Path("/tmp/omarchy-animation-visual")


def checked(argv):
  return subprocess.check_output(argv, text=True).strip()


def digest(path):
  return hashlib.file_digest(path.open("rb"), "sha256").hexdigest()


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--scenario", choices=("reference-success", "candidate-success", "candidate-failure", "candidate-fallback"), required=True)
args = parser.parse_args()
if (os.geteuid() != 0 or checked(["systemd-detect-virt", "--vm"]) not in {"qemu", "kvm"}
    or checked(["findmnt", "-n", "-o", "FSTYPE", "/"]) != "overlay"
    or not Path("/run/archiso/bootmnt/arch/x86_64/airootfs.sfs").is_file()
    or not Path("/root/.automated_script.benchmark-original.sh").is_file()):
  raise SystemExit("Visual fixture requires the disposable live builder")
directory = ROOT / args.scenario
directory.mkdir(exist_ok=False)
dashboard = ROOT / ("original-dashboard" if args.scenario.startswith("reference") else "candidate-dashboard")
expected = json.loads((ROOT / "source-manifest.json").read_text())
for name, sha in expected.items():
  if digest(ROOT / name) != sha:
    raise SystemExit("Staged visual source changed: " + name)
binary = directory / "bin"
binary.mkdir()
release = binary / "omarchy-release-install-target"
release.write_text('''#!/bin/bash
# Visual fixture only: no target exists and no installation is being released.
printf 'simulated-release\\n' >>"$OMARCHY_VISUAL_CALLS"
exit 0
''')
release.chmod(0o755)
env = {**os.environ, "PATH": str(binary) + ":" + os.environ["PATH"],
  "OMARCHY_DASHBOARD_TTY": "/dev/tty1", "OMARCHY_UI_INTERACTIVE": "no",
  "OMARCHY_UI_AUTO_REBOOT": "no", "OMARCHY_UI_FAILURE_ACTION": "exit",
  "OMARCHY_VISUAL_CALLS": str(directory / "fixture-calls.txt")}
mode = "failure" if args.scenario.endswith("failure") else "fallback" if args.scenario.endswith("fallback") else "success"
record = {"schema_version": 1, "purpose": "scripted actual-console visual test; no installation or speed measurement",
  "scenario": args.scenario, "dashboard_sha256": digest(dashboard), "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
  "real_ttfx": checked(["ttfx", "--version"]), "fake_command": "omarchy-release-install-target",
  "automatic_reboot_disabled": True, "started_monotonic_s": time.monotonic()}
capture = None
capture_log = None
if shutil.which("ffmpeg") and Path("/dev/fb0").exists():
  capture_log = (directory / "capture.log").open("w")
  command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning", "-f", "fbdev", "-framerate", "6",
    "-i", "/dev/fb0", "-t", "40", "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast",
    "-crf", "28", "-pix_fmt", "yuv420p", str(directory / "console.mp4")]
  capture = subprocess.Popen(command, stdout=capture_log, stderr=subprocess.STDOUT)
  record["capture_argv"] = command
  time.sleep(0.5)
command = ["bash", str(dashboard), str(directory / "dashboard.log"), str(directory / "state.json"), "--",
  "python3", str(ROOT / "visual-child.py"), "--directory", str(directory), "--mode", mode]
with (directory / "dashboard-process.log").open("w") as log:
  result = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=35)
record.update(dashboard_exit_status=result.returncode, finished_monotonic_s=time.monotonic(),
  expected_exit_status=17 if mode == "failure" else 0)
time.sleep(2)
if capture:
  if capture.poll() is None:
    capture.send_signal(signal.SIGINT)
  record["capture_exit_status"] = capture.wait(timeout=10)
  capture_log.close()
record["capture_available"] = (directory / "console.mp4").is_file() and (directory / "console.mp4").stat().st_size > 0
(directory / "result.json").write_text(json.dumps(record, indent=2) + "\n")
print(json.dumps(record), flush=True)
if result.returncode != record["expected_exit_status"]:
  raise SystemExit("Unexpected scripted dashboard exit; preserve visual failure evidence")
