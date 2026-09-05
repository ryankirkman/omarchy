#!/usr/bin/env python3
"""Boot an untimed, bounded KVM rescue VM with a failed target read-only."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

DIRECTORY = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('native_experiment', DIRECTORY / 'run-native-experiment.py')
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


def assert_stopped(run):
  for name in ('supervisor.pid', 'qemu.pid'):
    path = run / name
    if path.exists():
      try:
        os.kill(int(path.read_text()), 0)
      except ProcessLookupError:
        continue
      raise RuntimeError(f'Refusing rescue while {name} is still alive')


def readonly_target_args(target):
  if ',' in str(target) or '\n' in str(target):
    raise ValueError('Unsafe QEMU target filename')
  return ['-drive', f'file={target},if=none,id=rescue,format=qcow2,readonly=on,cache=none',
    '-device', 'virtio-blk-pci,drive=rescue,serial=OMARCHY_RESCUE']


def execute(args):
  assert_stopped(args.failed_run)
  target = args.failed_run / 'target.qcow2'
  if not target.is_file():
    raise ValueError('Failed installation has no target disk to inspect')
  if args.work.exists():
    raise ValueError('Rescue requires a fresh work directory')
  args.work.mkdir(parents=True)
  args.evidence.mkdir(parents=True, exist_ok=True)
  bench = args.repo / 'test/benchmarks'
  overlay = bench / 'install-speed/boot-overlay'
  key = args.work / 'rescue-key'
  native.command(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'disposable-readonly-rescue', '-f', key])
  payload = args.work / 'payload'
  scripts = payload / 'usr/local/lib/omarchy-benchmark'
  scripts.mkdir(parents=True)
  shutil.copyfile(key.with_suffix('.pub'), scripts / 'builder-key.pub')
  shutil.copyfile(DIRECTORY / 'collect-failed-install.py', scripts / 'collect-failed-install.py')
  rescue_initrd = args.work / 'initramfs-rescue.img'
  native.command([sys.executable, overlay / 'make-initramfs.py', '--initramfs', args.initrd,
    '--expected-initramfs-sha256', native.INITRD_SHA256, '--mode', 'builder',
    '--payload-dir', payload, '--preflight-script', overlay / 'builder-preflight.sh',
    '--output', rescue_initrd], timeout=60)
  shutil.copyfile(rescue_initrd.with_suffix('.img.manifest.json'), args.evidence / 'initramfs-rescue.manifest.json')
  run = args.work / 'vm'
  argv = [sys.executable, bench / 'iso-vm.py', 'run', '--mode', 'builder',
    '--iso', args.iso, '--iso-source', args.harness, '--run-dir', run,
    '--cpus', '4', '--memory', '8192', '--accelerator', 'kvm', '--poll-interval', '2',
    '--timeout', '270', '--kernel', args.kernel, '--initrd', rescue_initrd, '--append', native.CMDLINE,
    '--ssh-key', key, '--test-overlay-sha256', native.digest(rescue_initrd),
    '--extra-qemu-args-json', json.dumps(readonly_target_args(target))]
  status = {'schema_version': 1, 'measurement_valid': False, 'purpose': 'Read-only failure forensics',
    'failed_run': str(args.failed_run), 'status': 'starting', 'target_readonly': True}
  started = time.monotonic()
  native.save_json(args.evidence / 'rescue-status.json', status)
  with (args.evidence / 'rescue-supervisor.log').open('w') as log:
    process = subprocess.Popen(list(map(str, argv)), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
      deadline = time.monotonic() + 150
      while True:
        manifest = run / 'manifest.json'
        if manifest.exists() and json.loads(manifest.read_text()).get('status') == 'builder-ssh-ready':
          break
        if process.poll() is not None or time.monotonic() > deadline:
          raise RuntimeError('Read-only rescue VM did not become ready')
        time.sleep(1)
      response = native.mailbox(run, {'action': 'ssh',
        'command': 'python /usr/local/lib/omarchy-benchmark/collect-failed-install.py', 'timeout': 90}, timeout=100)['result']
      if response['returncode']:
        native.save_json(args.evidence / 'rescue-collector-error.json', response)
        raise RuntimeError('Read-only guest collector failed; see rescue-collector-error.json')
      data = response['stdout']
      if len(data.encode()) > 20 * 1024**2:
        raise RuntimeError('Rescue diagnostic exceeds the 20 MiB evidence limit')
      diagnostic = json.loads(data)
      native.save_json(args.evidence / 'installed-disk-diagnostics.json', diagnostic)
      status['status'] = 'collected'
      native.mailbox(run, {'action': 'ssh', 'command': 'systemctl poweroff', 'timeout': 10}, timeout=15)
      process.wait(timeout=20)
    except BaseException as error:
      status.update(status='failed', error_type=type(error).__name__, error=str(error))
      raise
    finally:
      native.stop_process_group(process, grace=5)
      native.collect_small(run, args.evidence / 'vm')
      status['host_rescue_seconds'] = time.monotonic() - started
      native.save_json(args.evidence / 'rescue-status.json', status)


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  for name in ('repo', 'work', 'evidence', 'failed-run', 'iso', 'harness', 'kernel', 'initrd'):
    parser.add_argument('--' + name, required=True, type=Path)
  args = parser.parse_args()
  for name, value in vars(args).items():
    setattr(args, name, value.resolve())
  execute(args)


if __name__ == '__main__':
  def interrupted(signum, frame):
    raise InterruptedError(f'Received signal {signum}')
  signal.signal(signal.SIGTERM, interrupted)
  signal.signal(signal.SIGINT, interrupted)
  main()
