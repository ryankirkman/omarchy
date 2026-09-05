#!/usr/bin/env python3
"""Run a fresh ISO installation in QEMU and retain independently checked evidence.

All VM state belongs outside a synced checkout. A filesystem mailbox permits QMP
and SSH control even when separate tool invocations have separate network namespaces.
"""

import argparse
from contextlib import ExitStack
import ctypes
import hashlib
import json
import mmap
import os
from pathlib import Path
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid


def write_json(path, value):
  temporary = path.with_name(path.name + ".tmp")
  temporary.write_text(json.dumps(value, indent=2) + "\n")
  temporary.replace(path)


def run_command(argv, *, env=None, timeout=60, input=None):
  return subprocess.run(argv, env=env, input=input, text=True, capture_output=True, timeout=timeout)


def require_success(result, description):
  if result.returncode:
    raise RuntimeError(f"{description}: {result.stderr}\n{result.stdout}")
  return result.stdout


def executable(name, prefix):
  for directory in (prefix / "usr/bin", prefix / "usr/sbin", prefix / "sbin"):
    if (directory / name).is_file():
      return str(directory / name)
  result = shutil.which(name)
  if not result:
    raise RuntimeError(f"Required executable unavailable: {name}")
  return result


def evict_and_measure_sources(sources):
  """Evict only these files and measure page residency without reading data."""
  if (sys.platform != "linux" or not callable(getattr(os, "posix_fadvise", None))
      or not hasattr(os, "POSIX_FADV_DONTNEED")):
    raise RuntimeError("Cold source cache requires Linux posix_fadvise and mincore")
  libc = ctypes.CDLL(None, use_errno=True)
  if not hasattr(libc, "mincore"):
    raise RuntimeError("Cold source cache requires Linux mincore")
  libc.mincore.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_ubyte))
  libc.mincore.restype = ctypes.c_int
  page_size = os.sysconf("SC_PAGE_SIZE")
  evidence = []
  with ExitStack() as stack:
    opened = []
    for source in sources:
      file = stack.enter_context(Path(source["path"]).open("rb"))
      info = os.fstat(file.fileno())
      if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        raise RuntimeError(f"Cold source cache requires a nonempty regular file: {source['path']}")
      opened.append((source, file, info.st_size))
    # Flush all sources before evicting any, and evict all before measuring any.
    # No host-wide drop_caches or writes to the source contents are needed.
    for _, file, _ in opened:
      os.fsync(file.fileno())
    for _, file, _ in opened:
      os.posix_fadvise(file.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    for source, file, file_bytes in opened:
      page_count = (file_bytes + page_size - 1) // page_size
      residency = (ctypes.c_ubyte * page_count)()
      # ACCESS_COPY supplies a private writable address for ctypes while the
      # descriptor stays read-only. Taking its address does not fault data in.
      with mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_COPY) as mapping:
        first_byte = ctypes.c_char.from_buffer(mapping)
        address = ctypes.addressof(first_byte)
        del first_byte
        if libc.mincore(address, file_bytes, residency) != 0:
          error = ctypes.get_errno()
          raise OSError(error, os.strerror(error), source["path"])
      evidence.append({**source, "file_bytes": file_bytes, "page_size": page_size,
                       "page_count": page_count,
                       "resident_pages": sum(page & 1 for page in residency),
                       "sampled_at_monotonic_s": time.monotonic()})
  return evidence


def build_cidata(args, env):
  # Reuse the official integration harness's exact configurator schema rather
  # than maintaining another archinstall configuration that can silently drift.
  harness = (args.iso_source / "test/integration.d/base-test.sh").read_text()
  start = harness.index("build_cidata() {")
  end = harness.index("\n# The dev/local ISO", start)
  key = args.run_dir / "id_ed25519"
  if args.ssh_key:
    shutil.copyfile(args.ssh_key, key)
    key.chmod(0o600)
    (key.with_suffix(".pub")).write_text(require_success(run_command(["ssh-keygen", "-y", "-f", str(key)]), "read SSH public key"))
  else:
    require_success(run_command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "disposable-omarchy-benchmark", "-f", str(key)]), "generate disposable SSH key")
  child_env = dict(env, BASE_DIR=str(args.run_dir), SSH_KEY=str(key),
                   CIDATA_IMG=str(args.run_dir / "cidata.img"),
                   GUEST_PASSWORD="omarchy", GUEST_USER="omarchy", GUEST_HOSTNAME="omarchy-benchmark",
                   RUNTIME_PACKAGE=args.runtime_package, SETTINGS_PACKAGE=args.settings_package)
  require_success(run_command(["bash", "-euo", "pipefail"], env=child_env, input=harness[start:end] + "\nbuild_cidata\n"), "generate cidata")
  return key


class QMPDisconnected(ConnectionError):
  """The QMP transport closed before a command response arrived."""


class Supervisor:
  def __init__(self, args):
    self.args = args
    self.directory = args.run_dir
    self.env = dict(os.environ)
    lib = str(args.toolchain / "usr/lib/x86_64-linux-gnu")
    self.env["LD_LIBRARY_PATH"] = lib + ":" + self.env.get("LD_LIBRARY_PATH", "")
    self.env["PATH"] = ":".join(str(args.toolchain / p) for p in ("usr/bin", "usr/sbin", "sbin")) + ":" + self.env["PATH"]
    self.env["OMP_THREAD_LIMIT"] = "1"
    self.env["QEMU_MODULE_DIR"] = str(args.toolchain / "usr/lib/x86_64-linux-gnu/qemu")
    self.qmp_socket = None
    self.qmp_stream = None
    self.qmp_events = []
    self.manifest = {}
    self.vm = None
    self.started = time.monotonic()
    self.collected = False
    self.restarted_for_installed_boot = False
    self.last_failed_probe_start = 0.0
    self.last_failed_probe_end = 0.0

  def connect_qmp(self):
    self.qmp_socket = socket.create_connection(("127.0.0.1", self.args.qmp_port), timeout=10)
    self.qmp_stream = self.qmp_socket.makefile("rwb", buffering=0)
    greeting = json.loads(self.qmp_stream.readline())
    if "QMP" not in greeting:
      raise RuntimeError(f"Invalid QMP greeting: {greeting}")
    self.qmp("qmp_capabilities")

  def qmp(self, command, arguments=None):
    identifier = uuid.uuid4().hex
    request = {"execute": command, "id": identifier}
    if arguments:
      request["arguments"] = arguments
    self.qmp_stream.write((json.dumps(request) + "\n").encode())
    while True:
      line = self.qmp_stream.readline()
      if not line:
        raise QMPDisconnected("QMP disconnected")
      response = json.loads(line)
      if "event" in response:
        self.qmp_events.append(response)
      if response.get("id") == identifier:
        if "error" in response:
          raise RuntimeError(str(response["error"]))
        return response.get("return")

  def qemu_stopped_after_disconnect(self, error, timeout=5):
    if self.vm.poll() is not None:
      return True
    if not isinstance(error, (QMPDisconnected, BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
      return False
    return self.wait_for_expected_qemu_exit(timeout)

  def wait_for_expected_qemu_exit(self, timeout=5):
    expected_exit = self.collected or (
      self.args.kernel and self.args.mode == "install" and not self.restarted_for_installed_boot
    )
    if not expected_exit:
      return False
    # QEMU can close QMP slightly before its process exits on -no-reboot or
    # final poweroff. Wait for actual exit, not a fixed delay or a retry that
    # could hide a persistently broken monitor. The host clock keeps running.
    try:
      return self.vm.wait(timeout=timeout) == 0
    except subprocess.TimeoutExpired:
      return False

  def qemu_stopped_during_install_shutdown(self, status, timeout=5):
    if (status != "shutdown" or not self.args.kernel or self.args.mode != "install"
        or self.restarted_for_installed_boot):
      return False
    return self.wait_for_expected_qemu_exit(timeout)

  def ssh(self, command, *, sudo=False, timeout=120):
    if sudo and self.args.guest_user != "root":
      command = "printf '%s\\n' omarchy | sudo -S -p '' bash -c " + shlex.quote(command)
    return run_command([
      "ssh", "-i", str(self.directory / "id_ed25519"), "-p", str(self.args.ssh_port),
      "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=no",
      "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=3", "-o", "LogLevel=ERROR",
      f"{self.args.guest_user}@127.0.0.1", command,
    ], timeout=timeout)

  def screenshot(self, name="latest-screen.png"):
    ppm = self.directory / "latest-screen.ppm"
    self.qmp("screendump", {"filename": str(ppm)})
    try:
      from PIL import Image
      with Image.open(ppm) as source:
        source.save(self.directory / name)
      ppm.unlink()
      return str(self.directory / name)
    except ImportError:
      return str(ppm)

  def record_failed_probe(self, result, started, finished):
    self.last_failed_probe_start = started
    self.last_failed_probe_end = finished
    write_json(self.directory / "last-failed-ssh-probe.json", {
      "started_host_wall_s": started, "finished_host_wall_s": finished,
      "returncode": result.returncode, "stderr": result.stderr[-16384:],
    })

  def timeout_reason(self, elapsed):
    if self.collected:
      return None
    boot_started = self.manifest.get("installed_boot_restart_host_wall_s")
    boot_timeout = self.args.installed_boot_timeout
    if boot_timeout is not None and boot_started is not None and elapsed - boot_started >= boot_timeout:
      return f"Installed system did not become SSH-ready within {boot_timeout}s after disk boot"
    if elapsed >= self.args.timeout:
      return f"Install timed out after {self.args.timeout}s"
    return None

  def fail_timeout(self, reason):
    # These keys change the guest's console. Mark the measurement invalid
    # before sending any input; a subsequently responding guest cannot rescue it.
    self.manifest.update(status="timeout", validation_passed=False, failure=reason,
                         measurement_failed_host_wall_s=time.monotonic() - self.started)
    write_json(self.directory / "manifest.json", self.manifest)
    self.qmp_socket.settimeout(2)
    diagnostics = {"reason": reason, "after_measurement_failure": True, "steps": []}
    def capture(label, action):
      step = {"label": label, "host_wall_s": time.monotonic() - self.started}
      try:
        step["result"] = action()
      except Exception as error:
        step["error"] = str(error)
      diagnostics["steps"].append(step)
      write_json(self.directory / "timeout-diagnostics.json", diagnostics)
    capture("usernet", lambda: self.qmp("human-monitor-command", {"command-line": "info usernet"}))
    capture("network", lambda: self.qmp("human-monitor-command", {"command-line": "info network"}))
    capture("before-keys", lambda: self.screenshot("timeout-before-keys.png"))
    for label, keys in (("escape", ("esc",)), ("tty2", ("ctrl", "alt", "f2"))):
      capture("send-" + label, lambda keys=keys: self.qmp("send-key", {
        "keys": [{"type": "qcode", "data": key} for key in keys], "hold-time": 100,
      }))
      time.sleep(1)
      capture("after-" + label, lambda label=label: self.screenshot("timeout-after-" + label + ".png"))
    capture("cpus", lambda: self.qmp("query-cpus-fast"))
    capture("registers", lambda: self.qmp("human-monitor-command", {"command-line": "info registers"}))
    raise RuntimeError(reason + "; failed measurement and diagnostic evidence retained")

  def collect_identity(self, prefix=""):
    evidence = {}
    for name, command in (
      ("machine-id", "cat /etc/machine-id"),
      ("ssh-host-fingerprints", 'for key in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -E sha256 -lf "$key"; done'),
      ("pacman-master-keys", "gpg --homedir /etc/pacman.d/gnupg --with-colons --list-secret-keys"),
      ("btrfs-uuid", "findmnt -n -o UUID /"),
      ("btrfs-subvolumes", "btrfs subvolume list -puq /; findmnt --json -t btrfs"),
      ("uki-files", "find /boot -type f -printf '%P %s bytes\\n' | LC_ALL=C sort"),
    ):
      result = self.ssh(command, sudo=True, timeout=180)
      (self.directory / (prefix + name + ".txt")).write_text(result.stdout)
      (self.directory / (prefix + name + ".stderr")).write_text(result.stderr)
      evidence[name] = require_success(result, "collect " + name)
    fingerprints = []
    primary = False
    for line in evidence["pacman-master-keys"].splitlines():
      fields = line.split(":")
      if fields[0] == "sec":
        primary = True
      elif fields[0] == "ssb":
        primary = False
      elif fields[0] == "fpr" and primary:
        fingerprints.append(fields[9])
        primary = False
    if len(fingerprints) != 1:
      raise RuntimeError(f"Expected one pacman local master signing identity, found {len(fingerprints)}")
    identity = {
      "machine_id": evidence["machine-id"].strip(),
      "ssh_host_key_fingerprints": [line.split()[1] for line in evidence["ssh-host-fingerprints"].splitlines() if line.strip()],
      "pacman_master_key_fingerprint": fingerprints[0],
      "btrfs_uuid": evidence["btrfs-uuid"].strip(),
    }
    write_json(self.directory / (prefix + "identity.json"), identity)
    return identity

  def standalone_media_plan(self, extra):
    """Only explicitly named CD-ROM extras can enter the optional proof."""
    if len(extra) % 2 or any(extra[index] not in {"-drive", "-device"} for index in range(0, len(extra), 2)):
      raise ValueError("Standalone reboot supports only explicit -drive/-device CD-ROM extras")
    drives, devices = {}, {}
    reserved = {"target", "iso", "cidata", "installer-cd", "cidata-usb"}
    for index in range(0, len(extra), 2):
      pieces = extra[index + 1].split(",")
      options = dict(piece.split("=", 1) for piece in pieces if "=" in piece)
      identifier = options.get("id")
      if not identifier or identifier in reserved or identifier in drives or identifier in devices:
        raise ValueError("Standalone reboot requires unique explicit IDs for every extra drive and device")
      if extra[index] == "-drive":
        if options.get("media") != "cdrom" or options.get("if") != "none" or "file" not in options:
          raise ValueError("Standalone reboot extra drives require media=cdrom,if=none,file=...")
        drives[identifier] = options
      else:
        if pieces[0] != "ide-cd":
          raise ValueError("Standalone reboot currently supports only ide-cd extra devices")
        devices[identifier] = options
    plan = [{"drive_id": "iso", "device_id": "installer-cd", "kind": "cdrom"}]
    for drive_id in drives:
      matching = [identifier for identifier, options in devices.items() if options.get("drive") == drive_id]
      if len(matching) != 1:
        raise ValueError("Every standalone extra drive requires exactly one named ide-cd device")
      plan.append({"drive_id": drive_id, "device_id": matching[0], "kind": "cdrom"})
    if len(devices) != len(drives):
      raise ValueError("Standalone extra devices must reference their explicit extra drives")
    plan.append({"drive_id": "cidata", "device_id": "cidata-usb", "kind": "usb-storage"})
    return plan

  def assert_standalone_media_absent(self, plan):
    blocks = self.qmp("query-block")
    for medium in plan:
      rows = [row for row in blocks if row.get("device") == medium["drive_id"]]
      if medium["kind"] == "cdrom":
        if len(rows) != 1 or rows[0].get("inserted") is not None:
          raise RuntimeError(f"CD-ROM medium remains present: {medium['drive_id']}")
      elif any(row.get("qdev") for row in rows):
        raise RuntimeError(f"CIDATA backend remains connected to a guest device: {rows}")
    return blocks

  def remove_standalone_media(self, plan):
    for medium in plan:
      if medium["kind"] == "cdrom":
        self.qmp("blockdev-open-tray", {"id": medium["device_id"], "force": True})
        self.qmp("blockdev-remove-medium", {"id": medium["device_id"]})
    event_start = len(self.qmp_events)
    self.qmp("device_del", {"id": "cidata-usb"})
    deadline = time.monotonic() + 30
    while True:
      # Each response also drains preceding asynchronous QMP events. The
      # DEVICE_DELETED event proves actual unplug, not just request acceptance.
      self.qmp("query-block")
      deleted = [event for event in self.qmp_events[event_start:]
                 if event.get("event") == "DEVICE_DELETED" and event.get("data", {}).get("device") == "cidata-usb"]
      if deleted:
        break
      if time.monotonic() >= deadline:
        raise RuntimeError("CIDATA device removal did not complete within 30s")
      time.sleep(0.1)
    return {"device_deleted_event": deleted[-1], "query_block": self.assert_standalone_media_absent(plan)}

  def verify_standalone_reboot(self, roots, identity):
    proof_path = self.directory / "standalone-reboot.json"
    proof = {
      "schema_version": 1, "passed": False,
      "outside_install_timing": True,
      "original_first_installed_ssh_wall_s": self.manifest["first_installed_ssh_wall_s"],
      "validation_started_host_wall_s": time.monotonic() - self.started,
      "qemu_pid_before": self.vm.pid, "root_before": roots, "identity_before": identity,
      "media_plan": self.manifest["standalone_media_plan"],
      "observed_ssh_disconnect": False,
    }
    self.manifest.update(status="validating-standalone-reboot", validation_passed=False,
                         standalone_reboot_passed=False)
    write_json(self.directory / "manifest.json", self.manifest)
    write_json(proof_path, proof)
    try:
      before = require_success(self.ssh("cat /proc/sys/kernel/random/boot_id"), "read original boot ID").strip()
      uuid.UUID(before)
      proof["boot_id_before"] = before
      proof["media_removal"] = self.remove_standalone_media(proof["media_plan"])
      proof["media_removed_host_wall_s"] = time.monotonic() - self.started
      write_json(proof_path, proof)
      reboot = self.ssh("systemctl reboot", sudo=True, timeout=20)
      proof["reboot_command"] = {"returncode": reboot.returncode, "stdout": reboot.stdout, "stderr": reboot.stderr}
      # An SSH connection may close after systemd accepted reboot. It cannot
      # establish success; a changed boot ID after observed downtime must do so.
      if reboot.returncode not in (0, 255):
        raise RuntimeError(f"Ordinary guest reboot failed: {reboot.stderr}")
      proof["reboot_requested_host_wall_s"] = time.monotonic() - self.started
      deadline = time.monotonic() + self.args.standalone_reboot_timeout
      while time.monotonic() < deadline:
        if self.vm.poll() is not None or self.vm.pid != proof["qemu_pid_before"]:
          raise RuntimeError("Standalone reboot requires the original QEMU process to remain alive")
        status = self.qmp("query-status")
        if status.get("status") != "running":
          raise RuntimeError(f"Unexpected QEMU state during standalone reboot: {status}")
        response = self.ssh("cat /proc/sys/kernel/random/boot_id", timeout=8)
        if response.returncode:
          proof["observed_ssh_disconnect"] = True
          proof["last_disconnected_host_wall_s"] = time.monotonic() - self.started
        elif response.stdout.strip() != before:
          after = response.stdout.strip()
          uuid.UUID(after)
          if not proof["observed_ssh_disconnect"]:
            raise RuntimeError("Boot ID changed without an observed SSH disconnection")
          proof["boot_id_after"] = after
          proof["standalone_ssh_ready_host_wall_s"] = time.monotonic() - self.started
          break
        write_json(proof_path, proof)
        time.sleep(1)
      else:
        raise RuntimeError(f"Standalone reboot did not become ready within {self.args.standalone_reboot_timeout}s")
      root = require_success(self.ssh("findmnt --json -o SOURCE,FSTYPE,TARGET /"), "check standalone installed root")
      (self.directory / "standalone-root.json").write_text(root)
      proof["root_after"] = json.loads(root).get("filesystems", [])
      if proof["root_after"] != roots:
        raise RuntimeError("Standalone reboot changed the installed root mount")
      proof["identity_after"] = self.collect_identity(prefix="standalone-")
      if proof["identity_after"] != identity:
        raise RuntimeError("Standalone reboot changed installed system identities")
      proof["media_after_reboot"] = self.assert_standalone_media_absent(proof["media_plan"])
      proof["qemu_pid_after"] = self.vm.pid
      if self.vm.poll() is not None or proof["qemu_pid_after"] != proof["qemu_pid_before"]:
        raise RuntimeError("QEMU process changed or exited before standalone validation completed")
      proof["passed"] = True
      proof["validation_finished_host_wall_s"] = time.monotonic() - self.started
      self.manifest["standalone_reboot_passed"] = True
    except Exception as error:
      proof["failure"] = str(error)
      self.manifest.update(status="standalone-reboot-failed", validation_passed=False)
      raise
    finally:
      write_json(proof_path, proof)
      write_json(self.directory / "manifest.json", self.manifest)

  def collect(self):
    root = require_success(self.ssh("findmnt --json -o SOURCE,FSTYPE,TARGET /"), "check installed root")
    (self.directory / "installed-root.json").write_text(root)
    roots = json.loads(root).get("filesystems", [])
    booted = any(row.get("fstype") == "btrfs" and row.get("source", "").startswith("/dev/vda2") for row in roots)
    if not booted:
      raise RuntimeError(f"SSH answered without the expected installed root: {root}")
    timing = require_success(self.ssh("cat /var/log/omarchy-install-timing.json", sudo=True), "collect installer timing")
    (self.directory / "install-timing.json").write_text(timing)
    for filename, command, sudo in (
      ("package-manifest.txt", "LC_ALL=C pacman -Q | LC_ALL=C sort", False),
      ("package-explicit.txt", "LC_ALL=C pacman -Qqe | LC_ALL=C sort", False),
      ("installed-boot.txt", "findmnt /boot; ls -l /boot; cat /proc/cmdline; uname -a", True),
      ("systemd-analyze-blame.txt", "systemd-analyze --no-pager blame", False),
      ("systemd-analyze-critical-chain.txt", "systemd-analyze --no-pager time; systemd-analyze --no-pager critical-chain", False),
      ("install.log", "cat /var/log/omarchy-install.log", True),
      ("journal-boot.log", "journalctl -b --no-pager", True),
    ):
      result = self.ssh(command, sudo=sudo, timeout=180)
      (self.directory / filename).write_text(result.stdout)
      (self.directory / (filename + ".stderr")).write_text(result.stderr)
      if filename.startswith("package-") and result.returncode:
        raise RuntimeError(f"Failed to collect {filename}: {result.stderr}")
    files = self.ssh("LC_ALL=C pacman -Qk", sudo=True, timeout=1200)
    (self.directory / "package-files.txt").write_text(files.stdout)
    (self.directory / "package-files.stderr").write_text(files.stderr)
    write_json(self.directory / "validation.json", {
      "booted_installed_root": booted, "package_files_exit_status": files.returncode,
      "root_mount": roots, "validated_at": time.time(),
    })
    identity = self.collect_identity()
    if self.args.verify_standalone_reboot:
      phases = json.loads(timing)
      if (files.returncode != 0 or not phases.get("finished_at")
          or phases.get("current_phase") != "Installation complete"
          or not phases.get("phases") or len(phases["phases"]) != phases.get("total_phases")
          or any(phase.get("status") != "ok" for phase in phases["phases"])):
        raise RuntimeError("Standalone reboot requires complete successful phases and package validation first")
      self.verify_standalone_reboot(roots, identity)
    self.manifest["status"] = "installed-and-booted"
    self.manifest["validation_passed"] = files.returncode == 0
    self.manifest["collected_at"] = time.time()
    self.collected = True
    self.screenshot("installed-screen.png")
    write_json(self.directory / "manifest.json", self.manifest)
    print(json.dumps({"event": "installed-and-booted", "package_files_exit_status": files.returncode,
                      "run_dir": str(self.directory)}), flush=True)

  def mailbox(self):
    for path in sorted((self.directory / "requests").glob("*.json")):
      response = self.directory / "responses" / path.name
      if response.exists():
        continue
      try:
        request = json.loads(path.read_text())
        action = request["action"]
        record = {"at_host_wall_s": time.monotonic() - self.started, **request}
        self.manifest.setdefault("interventions", []).append(record)
        if action == "qmp":
          result = self.qmp(request["execute"], request.get("arguments"))
          if request["execute"] in {"stop", "cont", "system_reset", "loadvm"} and not self.collected:
            self.manifest["measurement_interrupted"] = True
        elif action == "screenshot":
          result = self.screenshot(request.get("name", "requested-screen.png"))
        elif action == "ssh":
          child = self.ssh(request["command"], sudo=request.get("sudo", False), timeout=request.get("timeout", 120))
          result = {"returncode": child.returncode, "stdout": child.stdout, "stderr": child.stderr}
        elif action == "collect":
          self.collect()
          result = {"collected": True}
        else:
          raise ValueError(f"Unknown action: {action}")
        write_json(response, {"ok": True, "result": result})
      except Exception as error:
        write_json(response, {"ok": False, "error": str(error)})
      write_json(self.directory / "manifest.json", self.manifest)

  def extra_media(self, extra):
    devices = []
    drives = []
    for index, item in enumerate(extra):
      if item in {"-drive", "-device"} and index + 1 < len(extra):
        pieces = extra[index + 1].split(",")
        options = dict(piece.split("=", 1) for piece in pieces if "=" in piece)
        if item == "-drive":
          drives.append(options)
        else:
          devices.append((pieces[0], options, extra[index + 1]))
    result = []
    for drive in drives:
      if "file" not in drive:
        raise ValueError("Extra benchmark drives require an explicit file path")
      readonly = drive.get("readonly") in {"on", "yes", "true"} or drive.get("media") == "cdrom"
      if not readonly:
        if self.args.mode == "install":
          raise ValueError("Extra install benchmark media must be read-only; writable build disks require builder mode")
        continue
      path = Path(drive["file"]).resolve()
      drive_id = drive.get("id")
      matching = [(name, specification) for name, options, specification in devices if options.get("drive") == drive_id]
      interface = matching[0][0] if len(matching) == 1 else drive.get("if", "ide")
      device = matching[0][1] if len(matching) == 1 else None
      with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
      result.append({
        "path": str(path), "sha256": digest, "drive_id": drive_id,
        "format": drive.get("format", "auto"), "cache": drive.get("cache", "writeback"),
        "interface": interface, "device": device, "readonly": readonly,
      })
    return result

  def prepare_source_cache(self):
    if self.args.source_cache != "cold":
      return
    sources = [{"path": self.manifest["iso"], "sha256": self.manifest["iso_sha256"]}]
    sources.extend({"path": item["path"], "sha256": item["sha256"]} for item in self.manifest["extra_media"])
    if self.args.kernel:
      sources.extend([
        {"path": str(self.args.kernel.resolve()), "sha256": self.manifest["direct_kernel_sha256"]},
        {"path": str(self.args.initrd.resolve()), "sha256": self.manifest["direct_initrd_sha256"]},
      ])
    self.manifest["media_cache_preconditioning"] = "sha256-read-then-fsync-fadvise-dontneed-mincore-verified-cold-before-vm-start"
    records = evict_and_measure_sources(sources)
    self.manifest["source_cache_evidence"] = records
    warm = [item for item in records if item["resident_pages"] != 0]
    if warm:
      raise RuntimeError("Source cache remains resident after eviction: " + ", ".join(
        f"{item['path']} ({item['resident_pages']}/{item['page_count']} pages)" for item in warm))
    self.manifest["source_cache_verified_at_monotonic_s"] = time.monotonic()

  def start(self):
    args = self.args
    if self.directory.exists() and any(self.directory.iterdir()):
      raise RuntimeError("A fresh run requires an empty, new run directory")
    self.directory.mkdir(parents=True, exist_ok=True)
    (self.directory / "requests").mkdir()
    (self.directory / "responses").mkdir()
    key = build_cidata(args, self.env)
    disk = self.directory / "target.qcow2"
    qemu = executable("qemu-system-x86_64", args.toolchain)
    image = executable("qemu-img", args.toolchain)
    require_success(run_command([image, "create", "-f", "qcow2", str(disk), "40G"], env=self.env), "create fresh target")
    code = args.toolchain / "usr/share/OVMF/OVMF_CODE_4M.fd"
    variables = args.toolchain / "usr/share/OVMF/OVMF_VARS_4M.fd"
    shutil.copyfile(variables, self.directory / "OVMF_VARS_4M.fd")
    version = require_success(run_command([qemu, "--version"], env=self.env), "QEMU version").splitlines()[0]
    iso_hash = hashlib.file_digest(args.iso.open("rb"), "sha256").hexdigest()
    argv = [qemu, "-L", str(args.toolchain / "usr/share/qemu"),
      "-machine", f"q35,accel={args.accelerator}", "-cpu", "max" if args.accelerator == "tcg" else "host",
      "-smp", str(args.cpus), "-m", str(args.memory),
      "-drive", f"if=pflash,format=raw,readonly=on,file={code}",
      "-drive", f"if=pflash,format=raw,file={self.directory / 'OVMF_VARS_4M.fd'}",
      "-drive", f"file={disk},format=qcow2,if=none,id=target,cache=writeback",
      "-device", "virtio-blk-pci,drive=target,bootindex=1",
      "-device", f"virtio-vga,romfile={args.toolchain / 'usr/share/seabios/vgabios-virtio.bin'}",
      "-display", "none", "-usb", "-device", "usb-tablet",
      "-object", "rng-random,id=rng0,filename=/dev/urandom", "-device", "virtio-rng-pci,rng=rng0",
      "-netdev", f"user,id=net0,hostfwd=tcp:127.0.0.1:{args.ssh_port}-:22",
      "-device", "virtio-net-pci,netdev=net0,romfile=",
      "-qmp", f"tcp:127.0.0.1:{args.qmp_port},server=on,wait=off",
      "-serial", f"file:{self.directory / 'serial.log'}",
      "-drive", f"file={args.iso},media=cdrom,if=none,format=raw,id=iso,cache=writeback,readonly=on",
      "-device", "ide-cd,drive=iso,bootindex=2" + (",id=installer-cd" if args.verify_standalone_reboot else ""),
      "-drive", f"file={self.directory / 'cidata.img'},format=raw,if=none,id=cidata",
      "-device", "usb-storage,drive=cidata" + (",id=cidata-usb" if args.verify_standalone_reboot else "")]
    # The installed-system validation boots with no installation media or
    # test-only extra devices. Firmware must not fall back into autoinstall.
    installed_argv_template = []
    position = 0
    while position < len(argv):
      item = argv[position]
      if item in {"-drive", "-device"} and position + 1 < len(argv):
        value = argv[position + 1]
        if (item == "-drive" and ("id=iso," in value or value.endswith("id=iso") or "id=cidata" in value)) or (item == "-device" and ("drive=iso" in value or "drive=cidata" in value)):
          position += 2
          continue
      installed_argv_template.append(item)
      position += 1
    if args.kernel:
      argv.extend(["-kernel", str(args.kernel.resolve()), "-initrd", str(args.initrd.resolve()), "-append", args.append])
      if args.mode == "install":
        argv.append("-no-reboot")
    extra = []
    if args.extra_qemu_args_json:
      extra = json.loads(args.extra_qemu_args_json)
      if not isinstance(extra, list) or not all(isinstance(item, str) for item in extra):
        raise ValueError("extra QEMU arguments must be a JSON array of strings")
      argv.extend(extra)
    self.manifest = {
      "schema_version": 1, "status": "running", "mode": args.mode, "iso": str(args.iso), "iso_sha256": iso_hash,
      "qemu_version": version, "qemu_argv": argv, "accelerator": args.accelerator,
      "cpu_count": args.cpus, "memory_mib": args.memory, "fresh_target": True, "fresh_nvram": True,
      "disk_format": "qcow2", "disk_virtual_bytes": 40 * 1024 ** 3, "disk_cache": "writeback",
      "iso_cache": "writeback", "readiness_poll_interval_s": args.poll_interval,
      "installed_boot_timeout_s": args.installed_boot_timeout,
      "started_at": time.time(), "hostname": "omarchy-benchmark", "measurement_interrupted": False,
      "network": "QEMU user networking; ISO installs packages from its own bundled mirror",
      "encryption": False, "filesystem": "btrfs compress=zstd", "interventions": [],
      "cidata_configuration_sha256": hashlib.sha256((self.directory / "cidata/user_configuration.json").read_bytes()).hexdigest(),
      "test_overlay_sha256": args.test_overlay_sha256,
      "extra_media": self.extra_media(extra),
      "source_cache": args.source_cache,
      "media_cache_preconditioning": "sha256-read-iso-then-extra-media-in-array-order-then-kernel-then-initrd-before-vm-start",
      "direct_kernel_boot": bool(args.kernel),
      "direct_kernel_sha256": hashlib.sha256(args.kernel.read_bytes()).hexdigest() if args.kernel else None,
      "direct_initrd_sha256": hashlib.sha256(args.initrd.read_bytes()).hexdigest() if args.initrd else None,
      "direct_kernel_command_line": args.append if args.kernel else None,
      "reboot_strategy": "qemu-no-reboot-then-disk" if args.kernel and args.mode == "install" else "guest-firmware-reboot",
      "verify_standalone_reboot": args.verify_standalone_reboot,
      "standalone_reboot_timeout_s": args.standalone_reboot_timeout if args.verify_standalone_reboot else None,
    }
    if args.verify_standalone_reboot:
      self.manifest["reboot_strategy"] = "guest-firmware-reboot-with-standalone-validation"
      self.manifest["standalone_media_plan"] = self.standalone_media_plan(extra)
      self.manifest["standalone_reboot_passed"] = False
    self.prepare_source_cache()
    write_json(self.directory / "manifest.json", self.manifest)
    self.started = time.monotonic()
    self.manifest["vm_started_at_monotonic_s"] = self.started
    with (self.directory / "qemu.log").open("w") as log:
      self.vm = subprocess.Popen(argv, env=self.env, stdout=log, stderr=subprocess.STDOUT)
      (self.directory / "qemu.pid").write_text(str(self.vm.pid) + "\n")
      (self.directory / "supervisor.pid").write_text(str(os.getpid()) + "\n")
      for attempt in range(30):
        if self.vm.poll() is not None:
          raise RuntimeError("QEMU exited: " + (self.directory / "qemu.log").read_text())
        try:
          self.connect_qmp()
          break
        except (OSError, RuntimeError):
          time.sleep(1)
      else:
        raise RuntimeError("QMP did not become ready")
      print(json.dumps({"event": "started", "run_dir": str(self.directory), "pid": self.vm.pid}), flush=True)
      next_poll = 0
      while True:
        if self.vm.poll() is not None:
          if args.kernel and args.mode == "install" and not self.restarted_for_installed_boot and self.vm.returncode == 0:
            # -no-reboot exits when the live installer requests a reboot. Remove
            # direct-boot inputs before validating the installed disk, retaining
            # the original monotonic start and both writable disk/NVRAM files.
            previous_exit = self.vm.returncode
            shutil.copyfile(self.directory / "serial.log", self.directory / "live-serial.log")
            installed_argv = list(installed_argv_template)
            self.qmp_stream.close()
            self.qmp_socket.close()
            self.vm = subprocess.Popen(installed_argv, env=self.env, stdout=log, stderr=subprocess.STDOUT)
            (self.directory / "qemu.pid").write_text(str(self.vm.pid) + "\n")
            self.restarted_for_installed_boot = True
            self.manifest["installed_boot_qemu_argv"] = installed_argv
            self.manifest["installed_boot_restart_host_wall_s"] = time.monotonic() - self.started
            self.manifest["installer_qemu_exit_status"] = previous_exit
            write_json(self.directory / "manifest.json", self.manifest)
            for attempt in range(30):
              try:
                self.connect_qmp()
                break
              except (OSError, RuntimeError):
                if self.vm.poll() is not None:
                  raise RuntimeError("Installed-disk QEMU exited before QMP connected")
                time.sleep(1)
            else:
              raise RuntimeError("Installed-disk QMP did not become ready")
            print(json.dumps({"event": "restarted-for-installed-disk", "host_wall_s": time.monotonic() - self.started}), flush=True)
          else:
            break
        self.mailbox()
        elapsed = time.monotonic() - self.started
        if elapsed >= next_poll:
          next_poll = elapsed + args.poll_interval
          try:
            self.screenshot()
            vm_status = self.qmp("query-status")
          except (OSError, RuntimeError) as error:
            if self.qemu_stopped_after_disconnect(error):
              continue
            raise
          if self.qemu_stopped_during_install_shutdown(vm_status.get("status")):
            continue
          if not self.collected and vm_status.get("status") != "running":
            self.manifest["measurement_interrupted"] = True
            self.manifest.setdefault("unexpected_vm_states", []).append({"host_wall_s": elapsed, **vm_status})
            write_json(self.directory / "manifest.json", self.manifest)
          progress = {"event": "progress", "host_wall_s": elapsed,
                      "target_allocated_bytes": disk.stat().st_blocks * 512,
                      "host_free_bytes": shutil.disk_usage(self.directory).free,
                      "collected": self.collected, "vm_status": vm_status}
          write_json(self.directory / "progress.json", progress)
          print(json.dumps(progress), flush=True)
          if not self.collected:
            probe_started = time.monotonic() - self.started
            ready = self.ssh("true", timeout=8)
            probe_finished = time.monotonic() - self.started
            if ready.returncode != 0:
              self.record_failed_probe(ready, probe_started, probe_finished)
            if ready.returncode == 0:
              if args.mode == "builder":
                self.manifest["first_builder_ssh_wall_s"] = time.monotonic() - self.started
                self.manifest["status"] = "builder-ssh-ready"
                self.collected = True
                write_json(self.directory / "manifest.json", self.manifest)
                print(json.dumps({"event": "builder-ssh-ready", "run_dir": str(self.directory)}), flush=True)
              else:
                self.manifest["first_installed_ssh_wall_s"] = probe_finished
                self.manifest["last_failed_installed_ssh_probe_started_wall_s"] = self.last_failed_probe_start
                self.manifest["last_failed_installed_ssh_wall_s"] = self.last_failed_probe_end
                self.manifest["actual_readiness_uncertainty_s"] = probe_finished - self.last_failed_probe_start
                self.manifest["readiness_poll_uncertainty_s"] = probe_finished - self.last_failed_probe_start
                self.collect()
                if not args.keep_running:
                  self.ssh("systemctl poweroff", sudo=True)
          reason = self.timeout_reason(time.monotonic() - self.started)
          if reason:
            self.fail_timeout(reason)
        time.sleep(1)
      if not self.collected:
        self.manifest["status"] = "qemu-exited-before-validation"
      self.manifest["qemu_exit_status"] = self.vm.returncode
      write_json(self.directory / "manifest.json", self.manifest)


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  commands = parser.add_subparsers(dest="subcommand", required=True)
  run = commands.add_parser("run")
  run.add_argument("--mode", choices=("install", "builder"), default="install")
  run.add_argument("--ssh-key", type=Path, help="Existing disposable private key, copied into this run")
  run.add_argument("--guest-user", help="Defaults to omarchy for install mode, root for builder mode")
  run.add_argument("--iso", type=Path, required=True)
  run.add_argument("--iso-source", type=Path, required=True)
  run.add_argument("--run-dir", type=Path, required=True)
  run.add_argument("--toolchain", type=Path, default=Path("/"))
  run.add_argument("--cpus", type=int, default=4)
  run.add_argument("--memory", type=int, default=8192)
  run.add_argument("--accelerator", choices=("tcg", "kvm"), default="tcg")
  run.add_argument("--ssh-port", type=int, default=24022)
  run.add_argument("--qmp-port", type=int, default=24444)
  run.add_argument("--poll-interval", type=int, default=30)
  run.add_argument("--source-cache", choices=("conditioned", "cold"), default="conditioned",
                   help="Pre-read sources (default), or evict and verify zero cached pages before timing")
  run.add_argument("--timeout", type=int, default=7200)
  run.add_argument("--installed-boot-timeout", type=int,
                   help="Optional SSH readiness deadline after the direct installer restarts from disk")
  run.add_argument("--verify-standalone-reboot", action="store_true",
                   help="After a firmware install, require another ordinary reboot with all install media removed")
  run.add_argument("--standalone-reboot-timeout", type=int, default=600,
                   help="Separate post-install standalone reboot deadline (default: 600s)")
  run.add_argument("--runtime-package", default="omarchy")
  run.add_argument("--settings-package", default="omarchy-settings")
  run.add_argument("--keep-running", action="store_true")
  run.add_argument("--test-overlay-sha256", help="SHA256 of candidate test overlay; omitted for unmodified ISO")
  run.add_argument("--kernel", type=Path)
  run.add_argument("--initrd", type=Path)
  run.add_argument("--append", default="")
  run.add_argument("--extra-qemu-args-json", help="JSON argv array for extra devices; retain identical settings in comparison fixtures")
  request = commands.add_parser("request")
  request.add_argument("--run-dir", type=Path, required=True)
  request.add_argument("--json", required=True, help="JSON request, such as {\"action\":\"screenshot\"}")
  args = parser.parse_args()
  args.run_dir = args.run_dir.resolve()
  if args.subcommand == "request":
    identifier = uuid.uuid4().hex
    path = args.run_dir / "requests" / (identifier + ".json")
    write_json(path, json.loads(args.json))
    print(args.run_dir / "responses" / path.name)
    return
  for key in ("iso", "iso_source", "toolchain"):
    setattr(args, key, getattr(args, key).resolve())
  if not args.guest_user:
    args.guest_user = "root" if args.mode == "builder" else "omarchy"
  if bool(args.kernel) != bool(args.initrd):
    parser.error("--kernel and --initrd are required together")
  if args.installed_boot_timeout is not None and args.installed_boot_timeout <= 0:
    parser.error("--installed-boot-timeout must be positive")
  if args.verify_standalone_reboot and (args.kernel or args.mode != "install"):
    parser.error("--verify-standalone-reboot requires firmware boot without --kernel in install mode")
  if args.standalone_reboot_timeout <= 0:
    parser.error("--standalone-reboot-timeout must be positive")
  supervisor = Supervisor(args)
  try:
    supervisor.start()
  except BaseException as error:
    if supervisor.manifest:
      supervisor.manifest["failure"] = str(error)
      if not supervisor.collected and supervisor.manifest.get("status") == "running":
        supervisor.manifest["status"] = "failed"
      write_json(args.run_dir / "manifest.json", supervisor.manifest)
    if supervisor.vm and supervisor.vm.poll() is None:
      supervisor.vm.terminate()
      supervisor.vm.wait(timeout=30)
    raise


if __name__ == "__main__":
  main()
