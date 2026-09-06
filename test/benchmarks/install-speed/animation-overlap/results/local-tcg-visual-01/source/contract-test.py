#!/usr/bin/env python3
"""Run the pinned full dashboard with a PTY and harmless command fixtures.

Bash, jq, setsid, wait, and the eight-second timeout remain real. Installer
and effect handshakes establish overlap without an elapsed-time speed claim.
This does not validate ttfx's appearance, real disk release, or physical media.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import pty
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tty

from dashboard_patch import PIN, SOURCE_PATH, SOURCE_SHA256, patch_source


HERE = Path(__file__).resolve().parent
FINALIZING = "Finalizing boot and user setup"
LOGO = "FIXTURE LOGO\nSECOND ROW\n"
CSI = b"\x1b["

# Every executable reachable through PATH is either a listed real read-only
# utility or this fixture. In particular, no host release/reboot command can
# be reached. The visible tags are instrumentation, not dashboard output.
SHIM = r'''
import json
import os
from pathlib import Path
import sys
import time

root = Path(os.environ["CONTRACT_ROOT"])
settings = json.loads((root / "settings.json").read_text())
name = Path(sys.argv[0]).name
args = sys.argv[1:]

def events():
  return [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]

def record(event, visible=False, **details):
  value = {"event": event, "pid": os.getpid(), **details}
  fd = os.open(root / "events.jsonl", os.O_WRONLY | os.O_APPEND)
  try:
    os.write(fd, (json.dumps(value) + "\n").encode())
  finally:
    os.close(fd)
  if visible:
    fd = os.open(os.environ["OMARCHY_DASHBOARD_TTY"], os.O_WRONLY)
    try:
      os.write(fd, ("<contract:" + event + ">\n").encode())
    finally:
      os.close(fd)

def seen(event):
  return any(item["event"] == event for item in events())

def wait_for(predicate, description):
  deadline = time.monotonic() + 12
  while not predicate():
    if time.monotonic() >= deadline:
      record("fixture-timeout", description=description)
      sys.exit(97)
    time.sleep(0.01)

def progress_frames(after_effect=False):
  history = events()
  if after_effect:
    indices = [i for i, item in enumerate(history) if item["event"] == "effect-end"]
    if not indices:
      return 0
    history = history[indices[-1] + 1:]
  return sum(item["event"] == "progress-query" and item.get("phase") == settings["phase"]
             for item in history)

def publish(complete=False, failure=False):
  state = {
    "current_phase": "Installation complete" if complete else settings["phase"],
    "current_index": 7,
    "total_phases": 10,
    "started_at": 1,
    "finished_at": 2 if complete else 0,
    "duration_seconds": 9,
    "target": str(root / "target"),
    "phases": [{"name": "fixture finalizer", "status": "failed", "error": "contract child failed"}]
              if failure else [],
  }
  temporary = root / "state.next"
  temporary.write_text(json.dumps(state))
  temporary.replace(root / "state.json")

if name == "installer":
  (root / "installer.pid").write_text(str(os.getpid()))
  record("child-start")
  publish()
  if settings["overlap"]:
    wait_for(lambda: seen("effect-start"), "installer waiting for effect")
    record("child-advanced", visible=True)
    if settings["child_rc"]:
      publish(failure=True)
      record("child-done", visible=True, status=settings["child_rc"])
      sys.exit(settings["child_rc"])
    wait_for(lambda: progress_frames(after_effect=True) >= 2,
             "installer waiting for two finalizing progress frames after effect")
  else:
    wait_for(lambda: progress_frames() >= 2, "installer waiting for two progress frames")
  publish(complete=True)
  if settings["late_logo"]:
    # Change the phase before adding the logo: it must only be eligible for
    # the normal post-release effect, never an accidental early attempt.
    (root / "omarchy" / "logo.txt").write_text(settings["logo"])
  record("child-done", visible=True, status=0)
  sys.exit(0)

if name == "jq":
  if args and '.current_phase // "Starting installation"' in args[1]:
    current = json.loads((root / "state.json").read_text())
    record("progress-query", phase=current["current_phase"])
  os.execv(settings["real_jq"], [settings["real_jq"], *args])
elif name == "stty":
  if args != ["size"]:
    record("unexpected-command", command=name, args=args)
    sys.exit(98)
  print("24 80")
elif name == "ldd":
  pass
elif name == "timeout":
  record("timeout", args=args)
  os.execv(settings["real_timeout"], [settings["real_timeout"], *args])
elif name == "ttfx":
  if args == ["--version"]:
    record("warm-ttfx", visible=True)
  else:
    record("effect-start", visible=True, args=args)
    if settings["overlap"]:
      wait_for(lambda: seen("child-advanced"), "effect waiting for installer progress")
      if settings["child_rc"]:
        wait_for(lambda: seen("child-done"), "effect waiting for failed installer")
    record("effect-end", visible=True, status=settings["effect_rc"])
    sys.exit(settings["effect_rc"])
elif name == "omarchy-release-install-target":
  record("release", visible=True, args=args)
  sys.exit(settings["release_rc"])
elif name == "gum":
  if args == ["--version"]:
    record("warm-gum", visible=True)
  elif args and args[0] == "confirm":
    record("prompt", visible=True, args=args)
    sys.exit(settings["prompt_rc"])
  else:
    record("unexpected-command", command=name, args=args)
    sys.exit(98)
elif name in ("reboot", "systemctl", "poweroff"):
  record("shutdown", visible=True, command=name, args=args)
elif name == "omarchy-install-diagnose-media":
  record("diagnose", args=args)
else:
  record("unexpected-command", command=name, args=args)
  sys.exit(98)
'''


def require(condition, message):
  if not condition:
    raise AssertionError(message)


def tag(event):
  return f"<contract:{event}>".encode()


def ordered(items, *wanted):
  positions = [items.index(item) for item in wanted]
  require(positions == sorted(set(positions)), f"Incorrect order: {wanted}: {positions}")


def stop_processes(process, root):
  if process.poll() is None:
    process.terminate()
    try:
      process.wait(timeout=3)
    except subprocess.TimeoutExpired:
      os.killpg(process.pid, signal.SIGKILL)
      process.wait(timeout=3)
  pid_file = root / "installer.pid"
  if pid_file.exists():
    pid = int(pid_file.read_text())
    try:
      # The real dashboard starts this fixture with setsid. Clean up its
      # isolated group even if a failed assertion killed the supervisor.
      os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
      pass


def exercise(directory, dashboard, name, **changes):
  root = directory / name
  binary = root / "bin"
  binary.mkdir(parents=True)
  (root / "omarchy").mkdir()
  settings = {
    "phase": FINALIZING,
    "overlap": True,
    "child_rc": 0,
    "effect_rc": 0,
    "release_rc": 0,
    "prompt_rc": 0,
    "logo": LOGO,
    "initial_logo": True,
    "late_logo": False,
    "defer": False,
    "real_jq": shutil.which("jq"),
    "real_timeout": shutil.which("timeout"),
    **changes,
  }
  (root / "settings.json").write_text(json.dumps(settings))
  (root / "events.jsonl").write_text("")
  (root / "install.log").write_text("")
  (root / "state.json").write_text(json.dumps({
    "current_phase": "Starting installation", "duration_seconds": 9,
    "target": str(root / "target"),
  }))
  if settings["initial_logo"]:
    (root / "omarchy" / "logo.txt").write_text(LOGO)
  shim = root / "shim.py"
  shim.write_text(f"#!{sys.executable}\n" + SHIM)
  shim.chmod(0o755)
  for command in ("installer", "jq", "stty", "ldd", "timeout", "ttfx", "gum",
                  "omarchy-release-install-target", "omarchy-install-diagnose-media",
                  "systemctl", "reboot", "poweroff"):
    (binary / command).symlink_to(shim)
  for command in ("bash", "awk", "wc", "sed", "mkdir", "dirname", "setsid",
                  "grep", "sleep", "cat", "tail", "fold"):
    path = shutil.which(command)
    require(path is not None, f"Required utility unavailable: {command}")
    (binary / command).symlink_to(path)

  master, slave = pty.openpty()
  tty.setraw(slave)
  env = {key: value for key, value in os.environ.items()
         if not key.startswith("OMARCHY_") and key not in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS")}
  env.update({
    "PATH": str(binary), "CONTRACT_ROOT": str(root),
    "OMARCHY_PATH": str(root / "omarchy"),
    "OMARCHY_DASHBOARD_TTY": os.ttyname(slave),
    "OMARCHY_EXPECTED_PACKAGES_FILE": str(root / "no-packages"),
    "OMARCHY_FAILURE_TAIL_LOG": str(root / "install.log"),
    "OMARCHY_UI_FAILURE_ACTION": "exit",
    "OMARCHY_UI_DEFER_PROVISIONING": "yes" if settings["defer"] else "no",
    "OMARCHY_UI_AUTO_REBOOT": "yes", "OMARCHY_UI_INTERACTIVE": "yes",
    "LC_ALL": "C.UTF-8",
  })
  output = bytearray()
  process = None
  try:
    with (root / "stderr.txt").open("wb") as stderr:
      process = subprocess.Popen(
        [shutil.which("bash"), str(dashboard), str(root / "install.log"),
         str(root / "state.json"), "--", str(binary / "installer")],
        env=env, stdin=subprocess.DEVNULL, stdout=stderr, stderr=stderr,
        start_new_session=True,
      )
      deadline = time.monotonic() + 30
      with selectors.DefaultSelector() as selector:
        selector.register(master, selectors.EVENT_READ)
        while True:
          ready = selector.select(0.05)
          if ready:
            output.extend(os.read(master, 65536))
          elif process.poll() is not None:
            break
          require(time.monotonic() < deadline, f"{name}: dashboard timed out")
    (root / "tty.raw").write_bytes(output)
    events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    names = [event["event"] for event in events]
    ui = bytes(output)
    log = (root / "install.log").read_text()
    require(not any(event in names for event in ("fixture-timeout", "unexpected-command")),
            f"{name}: fixture failed: {events}")
    require(process.returncode == settings["child_rc"],
            f"{name}: expected status {settings['child_rc']}, got {process.returncode}; {log}")
    require(f"installer child exited with status {settings['child_rc']}" in log,
            f"{name}: real child wait/status was not observed")
    require(ui.endswith(CSI + b"0m" + CSI + b"?25h"), f"{name}: cleanup did not restore cursor")
    effects = [event for event in events if event["event"] == "effect-start"]
    expected_effects = 0 if settings["defer"] or not (settings["initial_logo"] or settings["late_logo"]) else 1
    require(len(effects) == expected_effects, f"{name}: expected {expected_effects} effects, got {len(effects)}")
    timeouts = [event for event in events if event["event"] == "timeout"]
    require(len(timeouts) == expected_effects, f"{name}: timeout/effect count differs")
    if effects:
      expected_args = ["-i", str(root / "omarchy" / "logo.txt"), "--canvas-width", "78",
                       "--anchor-text", "c", "--frame-rate", "260", "--reuse-canvas",
                       "--xterm-colors", "laseretch", "--final-gradient-stops", "2"]
      require(effects[0]["args"] == expected_args, f"{name}: full ttfx invocation changed")
      require(timeouts[0]["args"] == ["8s", "ttfx", *expected_args], f"{name}: eight-second cap changed")
      markers = [line.split() for line in log.splitlines() if line.startswith("OMARCHY_BENCHMARK_ANIMATION ")]
      mode = "finalizing" if settings["overlap"] else "complete"
      require(len(markers) == 2 and markers[0][1] == "begin" and markers[0][-1] == mode
              and markers[1][1] == "end" and markers[1][-1] == str(settings["effect_rc"]),
              f"{name}: missing/wrong real animation markers: {markers}")
    else:
      require("OMARCHY_BENCHMARK_ANIMATION " not in log, f"{name}: falsely recorded effect attempt")
    if settings["effect_rc"]:
      require(f"logo effect skipped (ttfx exited {settings['effect_rc']})" in log,
              f"{name}: effect failure status lost")

    if settings["overlap"]:
      ordered(names, "child-start", "effect-start", "child-advanced", "effect-end")
      ordered(ui, b"Finalizing Omarchy", b"Keep the install medium connected", tag("effect-start"))
      before_done = ui[:ui.index(tag("child-done"))]
      require(b"Installed Omarchy" not in before_done and b"You can now remove" not in before_done,
              f"{name}: completion or removal offered before installer finished")
      after_effect = ui[ui.index(tag("effect-end")):]
      restored = CSI + b"?25l" + CSI + b"2J" + CSI + b"H" + CSI + b"9;1H"
      require(restored in after_effect, f"{name}: unchanged-size progress frame was not restored")
      restored_frame = after_effect[after_effect.index(restored):]
      ordered(restored_frame, b"FIXTURE LOGO", CSI + b"12;1H", b"Installing Omarchy")
      if not settings["child_rc"]:
        end = names.index("effect-end")
        done = names.index("child-done")
        frames = [event for event in events[end + 1:done]
                  if event["event"] == "progress-query" and event["phase"] == FINALIZING]
        require(len(frames) >= 2, f"{name}: did not exercise repeated finalizing polls")
    else:
      require(b"Finalizing Omarchy" not in ui, f"{name}: unexpected early finish frame")

    if settings["child_rc"]:
      require(not any(event in names for event in ("release", "warm-ttfx", "warm-gum", "prompt", "shutdown")),
              f"{name}: failure continued to release/success/reboot")
      ordered(names, "effect-start", "child-done", "effect-end", "diagnose")
      require(b"Omarchy installation stopped" in ui and b"Installer exited with status 37" in ui,
              f"{name}: missing failure screen/status")
      require("failed phase: fixture finalizer: contract child failed" in log,
              f"{name}: failure diagnosis lost")
      require(b"Installed Omarchy" not in ui and b"You can now remove" not in ui,
              f"{name}: failure displayed a success/removal promise")
    elif settings["defer"]:
      require(not any(event in names for event in ("release", "warm-ttfx", "warm-gum", "prompt")),
              f"{name}: deferred install took direct-install finish path")
      require(b"Installed Omarchy" not in ui and b"You can now remove" not in ui,
              f"{name}: deferred install displayed success/removal")
      shutdown = [event for event in events if event["event"] == "shutdown"]
      require(len(shutdown) == 1 and shutdown[0]["command"] == "reboot" and shutdown[0]["args"] == [],
              f"{name}: deferred reboot path changed")
      ordered(names, "child-done", "shutdown")
    else:
      ordered(names, "child-done", "release", "warm-ttfx", "warm-gum", "prompt")
      # Duration formatting belongs to the pinned implementation. In
      # particular, host jq versions can take its existing "Complete"
      # fallback; these contracts concern the success frame's ordering.
      ordered(ui, tag("child-done"), tag("release"), tag("warm-gum"), b"Installed Omarchy in ", tag("prompt"))
      if effects and not settings["overlap"]:
        ordered(names, "release", "warm-gum", "effect-start", "effect-end", "prompt")
      releases = [event for event in events if event["event"] == "release"]
      require(len(releases) == 1 and releases[0]["args"] == [str(root / "target")], f"{name}: wrong release target")
      note = b"Leave the install medium in until the reboot completes" if settings["release_rc"] else b"You can now remove the install medium"
      ordered(ui, tag("warm-gum"), note, tag("prompt"))
      if settings["release_rc"]:
        require(b"You can now remove" not in ui, f"{name}: failed release offered medium removal")
      shutdown = [event for event in events if event["event"] == "shutdown"]
      if settings["prompt_rc"]:
        require(not shutdown, f"{name}: declined prompt still rebooted")
      else:
        expected = ["reboot"] if settings["release_rc"] else ["reboot", "-ff"]
        require(len(shutdown) == 1 and shutdown[0]["command"] == "systemctl" and shutdown[0]["args"] == expected,
                f"{name}: reboot decision changed")
        ordered(names, "prompt", "shutdown")
    print(f"PASS {name}", flush=True)
  finally:
    if process is not None:
      stop_processes(process, root)
    (root / "tty.raw").write_bytes(output)
    os.close(master)
    os.close(slave)


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--iso-source", type=Path, required=True)
  args = parser.parse_args()
  serial = Path("/dev/ttyS0")
  require(not serial.exists() or not stat.S_ISCHR(serial.stat().st_mode),
          "Run this host contract where /dev/ttyS0 is absent; real benchmark markers must not write a host serial port")
  require(shutil.which("jq") is not None and shutil.which("timeout") is not None,
          "Real jq and GNU timeout are required")
  source = subprocess.run(["git", "-C", str(args.iso_source), "show", f"{PIN}:{SOURCE_PATH}"],
                          check=True, capture_output=True).stdout
  require(hashlib.sha256(source).hexdigest() == SOURCE_SHA256, "Pinned source SHA256 mismatch")
  patched = patch_source(source)
  try:
    patch_source(source + b"\n")
  except ValueError:
    pass
  else:
    raise AssertionError("Source drift was accepted")
  suffix = b'set +e\nwait "$child_pid"\n'
  require(source.count(suffix) == patched.count(suffix) == 1
          and source.split(suffix)[1] == patched.split(suffix)[1],
          "Post-child wait/status/failure/release/warm/prompt/reboot tail changed")
  subprocess.run(["bash", "-n"], input=patched, check=True)
  temporary = Path(tempfile.mkdtemp(prefix="omarchy-animation-contract-"))
  try:
    dashboard = temporary / "dashboard.sh"
    dashboard.write_bytes(patched)
    exercise(temporary, dashboard, "overlap-and-redraw")
    exercise(temporary, dashboard, "failure-during-effect", child_rc=37)
    exercise(temporary, dashboard, "phase-unobserved-fallback", phase="Configuring system", overlap=False)
    exercise(temporary, dashboard, "late-logo-fallback", initial_logo=False, late_logo=True, overlap=False)
    exercise(temporary, dashboard, "missing-logo", initial_logo=False, overlap=False)
    exercise(temporary, dashboard, "deferred", defer=True, overlap=False)
    exercise(temporary, dashboard, "effect-error", effect_rc=23)
    exercise(temporary, dashboard, "effect-timeout-status", effect_rc=124)
    exercise(temporary, dashboard, "release-failure", release_rc=1)
    exercise(temporary, dashboard, "declined-prompt", prompt_rc=1)
    subprocess.run([sys.executable, str(HERE / "payload-contract-test.py"),
                    "--iso-source", str(args.iso_source)], check=True)
  except BaseException:
    print(f"Contract failure evidence retained at {temporary}", file=sys.stderr)
    raise
  else:
    shutil.rmtree(temporary)
  print("PASS pinned full-dashboard contracts and payload activation contracts", flush=True)


if __name__ == "__main__":
  main()
