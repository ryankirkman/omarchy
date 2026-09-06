#!/usr/bin/env python3
"""Stage small visual fixtures and retain real QEMU console frames via mailbox."""

import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile
import time


def save(path, value):
  temporary = path.with_name(path.name + ".tmp")
  temporary.write_text(json.dumps(value, indent=2) + "\n")
  temporary.replace(path)


def request(run, name, command, timeout=30):
  path = run / "vm/requests" / (name + ".json")
  response = run / "vm/responses" / path.name
  if path.exists() or response.exists():
    raise RuntimeError("Fresh mailbox name required")
  save(path, {"action": "ssh", "command": command, "timeout": timeout})
  deadline = time.monotonic() + timeout + 15
  while not response.exists():
    if time.monotonic() > deadline:
      raise TimeoutError(name)
    time.sleep(0.2)
  result = json.loads(response.read_text())
  if not result.get("ok") or result["result"]["returncode"]:
    raise RuntimeError(f"Mailbox command failed: {name}: {result}")
  return result["result"]["stdout"]


def capture_frame(run, frames, records, previous):
  source = run / "vm/latest-screen.png"
  progress = json.loads((run / "vm/progress.json").read_text())
  instant = progress["host_wall_s"]
  if instant == previous or not source.exists():
    return previous
  data = source.read_bytes()
  name = f"frame-{len(records):04}.png"
  (frames / name).write_bytes(data)
  records.append({"file": name, "observed_host_monotonic_s": time.monotonic(),
    "supervisor_progress_host_wall_s": instant, "sha256": hashlib.sha256(data).hexdigest()})
  return instant


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--directory", type=Path, required=True)
  args = parser.parse_args()
  run = args.directory.resolve()
  if not run.is_relative_to(Path("/tmp")):
    raise ValueError("Mutable visual state belongs below /tmp")
  deadline = time.monotonic() + 600
  while True:
    path = run / "vm/manifest.json"
    if path.exists() and json.loads(path.read_text()).get("status") == "builder-ssh-ready":
      break
    if time.monotonic() > deadline:
      raise TimeoutError("Builder SSH did not become ready")
    time.sleep(1)
  archive_data = io.BytesIO()
  with tarfile.open(fileobj=archive_data, mode="w:gz") as archive:
    for name in ("original-dashboard", "candidate-dashboard", "visual-child.py", "visual-guest.py", "source-manifest.json"):
      archive.add(run / "source" / name, arcname=name)
  encoded = base64.b64encode(archive_data.getvalue()).decode()
  stage = "test ! -e /tmp/omarchy-animation-visual; mkdir -m700 /tmp/omarchy-animation-visual; "
  stage += "printf %s " + shlex.quote(encoded) + " | base64 -d | tar -xzf - -C /tmp/omarchy-animation-visual"
  request(run, "visual-01-stage", "bash -euo pipefail -c " + shlex.quote(stage))
  available = request(run, "visual-02-availability", "bash -c " + shlex.quote(
    "for c in ttfx gum ffmpeg updatedb plocate; do command -v $c || true; done; "
    "test ! -e /dev/fb0 || ls -l /dev/fb0; stty size </dev/tty1; pgrep -af '^python -m orchestrator.main$' || true"))
  (run / "guest-availability.txt").write_text(available)
  print(json.dumps({"event": "guest-ready", "availability": available}), flush=True)
  for index, scenario in enumerate(("reference-success", "candidate-success", "candidate-failure", "candidate-fallback")):
    output = run / scenario
    output.mkdir(exist_ok=False)
    frames = output / "frames"
    frames.mkdir()
    command = "systemd-run --unit=omarchy-visual-" + scenario + " --property=Type=exec /usr/bin/python3 "
    command += "/tmp/omarchy-animation-visual/visual-guest.py --scenario " + scenario
    request(run, f"visual-{index + 3:02}-start", command)
    print(json.dumps({"event": "scenario-started", "scenario": scenario}), flush=True)
    records = []
    previous = None
    finish = time.monotonic() + 55
    next_probe = time.monotonic() + 8
    probe = 0
    while True:
      previous = capture_frame(run, frames, records, previous)
      if time.monotonic() > finish:
        raise TimeoutError("Visual scenario did not finish: " + scenario)
      if time.monotonic() >= next_probe:
        probe += 1
        next_probe = time.monotonic() + 5
        path = "/tmp/omarchy-animation-visual/" + scenario + "/result.json"
        result = request(run, f"visual-{index + 3:02}-probe-{probe:02}",
          "if test -f " + path + "; then cat " + path + "; else echo PENDING; fi")
        if result.strip() != "PENDING":
          save(output / "result.json", json.loads(result))
          break
      time.sleep(0.2)
    save(output / "frames.json", {"purpose": "QMP console frames sampled by unchanged supervisor; not native timing", "frames": records})
    collector = "import base64,io,tarfile,pathlib; p=pathlib.Path('/tmp/omarchy-animation-visual/" + scenario + "'); "
    collector += "b=io.BytesIO(); a=tarfile.open(fileobj=b,mode='w:gz'); "
    collector += "[a.add(f,arcname=f.name) for f in p.iterdir() if f.is_file()]; a.close(); print(base64.b64encode(b.getvalue()).decode())"
    data = request(run, f"visual-{index + 3:02}-collect", "python3 -c " + shlex.quote(collector))
    decoded = base64.b64decode(data)
    with tarfile.open(fileobj=io.BytesIO(decoded), mode="r:gz") as archive:
      for item in archive:
        if not item.isfile() or Path(item.name).name != item.name:
          raise ValueError("Unexpected visual evidence archive member")
        (output / item.name).write_bytes(archive.extractfile(item).read())
    # A short 1-fps QMP recording remains available when the live ISO has no
    # ffmpeg framebuffer capture. Frame timestamps retain the sampling limits.
    if records:
      subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-framerate", "1",
        "-i", str(frames / "frame-%04d.png"), "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast",
        "-crf", "25", "-pix_fmt", "yuv420p", str(output / "qmp-console.mp4")], check=True, timeout=30)
    print(json.dumps({"event": "scenario-collected", "scenario": scenario,
      "result": json.loads((output / "result.json").read_text()), "qmp_frames": len(records)}), flush=True)
  print(json.dumps({"event": "visual-fixtures-complete", "vm_left_running_for_bounded_followup": True}), flush=True)


if __name__ == "__main__":
  main()
