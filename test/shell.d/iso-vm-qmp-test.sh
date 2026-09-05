#!/bin/bash

set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/base-test.sh"

python3 - "$ROOT" <<'PYTEST'
import importlib.util
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location("iso_vm", Path(sys.argv[1]) / "test/benchmarks/iso-vm.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ClosedMonitor:
  def __init__(self, pipe):
    self.pipe = pipe
  def write(self, _request):
    pass
  def readline(self):
    return self.pipe.readline()


def exercise(delay, exit_status, *, timeout=1, direct=True, collected=False, shutdown=False):
  # Real EOF arrives while this child deliberately remains alive. No QEMU,
  # disks or synthetic performance measurements are involved in this test.
  child = subprocess.Popen(
    [sys.executable, "-c", f"import os,time; os.close(1); time.sleep({delay}); os._exit({exit_status})"],
    stdout=subprocess.PIPE,
  )
  supervisor = object.__new__(module.Supervisor)
  supervisor.vm = child
  supervisor.args = SimpleNamespace(kernel=Path("kernel") if direct else None, mode="install")
  supervisor.collected = collected
  supervisor.restarted_for_installed_boot = collected
  supervisor.qmp_stream = ClosedMonitor(child.stdout)
  try:
    try:
      supervisor.qmp("query-status")
    except module.QMPDisconnected as error:
      assert child.poll() is None, "fixture did not reproduce EOF-before-process-exit"
      started = time.monotonic()
      accepted = (supervisor.qemu_stopped_during_install_shutdown("shutdown", timeout=timeout) if shutdown
                  else supervisor.qemu_stopped_after_disconnect(error, timeout=timeout))
      elapsed = time.monotonic() - started
      return accepted, elapsed, child.poll()
    raise AssertionError("QMP accepted EOF as a command response")
  finally:
    if child.poll() is None:
      child.kill()
    child.wait()
    child.stdout.close()


accepted, elapsed, status = exercise(0.15, 0)
assert accepted and status == 0, "clean delayed reboot exit was rejected"
assert elapsed < 1, "successful exit waited for the entire grace period"
accepted, elapsed, status = exercise(2, 0, timeout=0.03)
assert not accepted and status is None and elapsed < 0.5, "persistent QMP loss was ignored or unbounded"
accepted, _, status = exercise(0.1, 7)
assert not accepted and status == 7, "failed QEMU exit was accepted as normal reboot"
accepted, elapsed, status = exercise(2, 0, direct=False)
assert not accepted and status is None and elapsed < 0.5, "unexpected live-guest disconnect was accepted"
accepted, _, status = exercise(0.1, 0, direct=False, collected=True)
assert accepted and status == 0, "normal final poweroff has the same EOF race"

accepted, _, status = exercise(0.1, 0, shutdown=True)
assert accepted and status == 0, "reported shutdown before process exit was rejected"
accepted, _, status = exercise(2, 0, direct=False, shutdown=True)
assert not accepted and status is None, "shutdown outside direct install transition was accepted"

supervisor = object.__new__(module.Supervisor)
supervisor.vm = SimpleNamespace(poll=lambda: None, wait=lambda **_: (_ for _ in ()).throw(AssertionError("unexpected wait")))
assert not supervisor.qemu_stopped_after_disconnect(RuntimeError("QMP command rejected")), "protocol errors must not enter the exit grace path"
for status in ("paused", "io-error", "internal-error"):
  assert not supervisor.qemu_stopped_during_install_shutdown(status), "genuine VM failure entered normal shutdown handling"
print("ok - QMP EOF waits for actual expected process exit and preserves genuine failures")
PYTEST
