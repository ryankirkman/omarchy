#!/usr/bin/env python3
"""Run a fresh ISO installation in QEMU and retain independently checked evidence.

All VM state belongs outside a synced checkout. A filesystem mailbox permits QMP
and SSH control even when separate tool invocations have separate network namespaces.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
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
        raise RuntimeError("QMP disconnected")
      response = json.loads(line)
      if response.get("id") == identifier:
        if "error" in response:
          raise RuntimeError(str(response["error"]))
        return response.get("return")

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
      "-device", "ide-cd,drive=iso,bootindex=2",
      "-drive", f"file={self.directory / 'cidata.img'},format=raw,if=none,id=cidata",
      "-device", "usb-storage,drive=cidata"]
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
      "started_at": time.time(), "hostname": "omarchy-benchmark", "measurement_interrupted": False,
      "network": "QEMU user networking; ISO installs packages from its own bundled mirror",
      "encryption": False, "filesystem": "btrfs compress=zstd", "interventions": [],
      "cidata_configuration_sha256": hashlib.sha256((self.directory / "cidata/user_configuration.json").read_bytes()).hexdigest(),
      "test_overlay_sha256": args.test_overlay_sha256,
      "direct_kernel_boot": bool(args.kernel),
      "direct_kernel_sha256": hashlib.sha256(args.kernel.read_bytes()).hexdigest() if args.kernel else None,
      "direct_initrd_sha256": hashlib.sha256(args.initrd.read_bytes()).hexdigest() if args.initrd else None,
      "direct_kernel_command_line": args.append if args.kernel else None,
      "reboot_strategy": "qemu-no-reboot-then-disk" if args.kernel and args.mode == "install" else "guest-firmware-reboot",
    }
    write_json(self.directory / "manifest.json", self.manifest)
    self.started = time.monotonic()
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
          except (OSError, RuntimeError):
            if self.vm.poll() is not None:
              continue
            raise
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
              self.last_failed_probe_start = probe_started
              self.last_failed_probe_end = probe_finished
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
          if elapsed > args.timeout and not self.collected:
            self.manifest["status"] = "timeout"
            write_json(self.directory / "manifest.json", self.manifest)
            raise RuntimeError(f"Install timed out after {args.timeout}s; evidence retained")
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
  run.add_argument("--timeout", type=int, default=7200)
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
