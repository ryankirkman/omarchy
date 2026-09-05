#!/bin/bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/base-test.sh"
python3 - "$ROOT" <<'PY'
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('iso_vm_standalone', Path(sys.argv[1]) / 'test/benchmarks/iso-vm.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
before = '11111111-1111-4111-8111-111111111111'
after = '22222222-2222-4222-8222-222222222222'
roots = [{'source': '/dev/vda2[/@]', 'fstype': 'btrfs', 'target': '/'}]
identity = {'machine_id': 'a' * 32, 'ssh_host_key_fingerprints': ['SHA256:test'],
            'pacman_master_key_fingerprint': 'B' * 40, 'btrfs_uuid': before}

def supervisor(directory):
  instance = object.__new__(module.Supervisor)
  instance.directory = directory
  instance.qmp_events = []
  instance.args = SimpleNamespace(standalone_reboot_timeout=10)
  instance.manifest = {'status': 'running', 'first_installed_ssh_wall_s': 42.125,
                       'standalone_media_plan': instance.standalone_media_plan([])}
  instance.started = time.monotonic() - 50
  instance.collected = False
  instance.vm = SimpleNamespace(pid=123, poll=lambda: None)
  return instance

with tempfile.TemporaryDirectory(prefix='omarchy-standalone-test-') as temporary:
  directory = Path(temporary)
  instance = supervisor(directory)
  extra = ['-drive', 'file=/tmp/image.iso,media=cdrom,if=none,id=rootimage',
           '-device', 'ide-cd,drive=rootimage,id=rootimage-cd']
  plan = instance.standalone_media_plan(extra)
  assert [item['device_id'] for item in plan] == ['installer-cd', 'rootimage-cd', 'cidata-usb']
  for invalid in (['-cdrom', '/tmp/image.iso'], ['-device', 'virtio-blk-pci,drive=x,id=y'],
                  extra[:-1] + ['ide-cd,drive=rootimage'], extra[:-1] + ['ide-cd,drive=rootimage,id=iso'],
                  ['-drive', 'file=/tmp/x,media=disk,if=none,id=x'],
                  ['-drive', 'file=/tmp/x,media=cdrom,if=none,id=x']):
    try:
      instance.standalone_media_plan(invalid)
    except ValueError:
      pass
    else:
      raise AssertionError(f'unsupported/ambiguous media accepted: {invalid}')

  calls = []
  def monitor(command, arguments=None):
    calls.append((command, arguments))
    if command == 'device_del':
      instance.qmp_events.append({'event': 'DEVICE_DELETED', 'data': {'device': 'cidata-usb'}})
    if command == 'query-block':
      return [{'device': 'iso', 'qdev': '/machine/peripheral/installer-cd'}, {'device': 'cidata'}]
  instance.qmp = monitor
  evidence = instance.remove_standalone_media(instance.manifest['standalone_media_plan'])
  assert evidence['device_deleted_event']['data']['device'] == 'cidata-usb'
  assert calls[:3] == [('blockdev-open-tray', {'id': 'installer-cd', 'force': True}),
                       ('blockdev-remove-medium', {'id': 'installer-cd'}), ('device_del', {'id': 'cidata-usb'})]
  for bad in ([{'device': 'iso', 'inserted': {'file': 'still.iso'}}],
              [{'device': 'iso'}, {'device': 'cidata', 'qdev': '/machine/peripheral/cidata-usb'}], []):
    instance.qmp = lambda *_: bad
    try:
      instance.assert_standalone_media_absent(instance.manifest['standalone_media_plan'])
    except RuntimeError:
      pass
    else:
      raise AssertionError('media remaining attached accepted')

  def exercise(*, disconnect=True, changed_identity=False, failed_process=False):
    instance = supervisor(directory)
    responses = [before] + ([None] if disconnect else []) + [after]
    def ssh(command, **kwargs):
      if command == 'cat /proc/sys/kernel/random/boot_id':
        value = responses.pop(0)
        return subprocess.CompletedProcess([], 255 if value is None else 0, value or '', '')
      if command == 'systemctl reboot':
        if failed_process:
          instance.vm.poll = lambda: 1
        return subprocess.CompletedProcess([], 0, '', '')
      if command.startswith('findmnt'):
        return subprocess.CompletedProcess([], 0, json.dumps({'filesystems': roots}), '')
      raise AssertionError(command)
    instance.ssh = ssh
    instance.qmp = lambda *_: {'status': 'running'}
    instance.remove_standalone_media = lambda _: {'device_deleted_event': {'event': 'DEVICE_DELETED'}, 'query_block': []}
    instance.assert_standalone_media_absent = lambda _: []
    instance.collect_identity = lambda **_: {**identity, 'machine_id': 'changed'} if changed_identity else identity
    with patch.object(module.time, 'sleep'):
      try:
        instance.verify_standalone_reboot(roots, identity)
      except RuntimeError:
        assert disconnect is False or changed_identity or failed_process
        assert not instance.manifest['validation_passed']
      else:
        assert disconnect and not changed_identity and not failed_process
    proof = json.loads((directory / 'standalone-reboot.json').read_text())
    assert proof['passed'] == (disconnect and not changed_identity and not failed_process)
    assert instance.manifest['first_installed_ssh_wall_s'] == 42.125
    assert proof['original_first_installed_ssh_wall_s'] == 42.125
    assert not instance.collected, 'gate must not independently accept a run'
    if proof['passed']:
      assert proof['boot_id_before'] != proof['boot_id_after']
      assert proof['qemu_pid_before'] == proof['qemu_pid_after'] == 123
      assert proof['identity_before'] == proof['identity_after'] == identity
    return proof
  exercise()
  exercise(disconnect=False)
  exercise(changed_identity=True)
  exercise(failed_process=True)

  # Reproduce a successful initial boot followed by a standalone SSH timeout.
  # Persist failure before diagnostic keys, and keep the original timing/probe.
  instance = supervisor(directory)
  instance.args.standalone_reboot_timeout = 2
  original = {
    'last-failed-ssh-probe.json': b'initial readiness probe',
    'timeout-diagnostics.json': b'initial diagnostics',
    'timeout-before-keys.png': b'initial screenshot',
  }
  for name, data in original.items():
    (directory / name).write_bytes(data)
  instance.last_failed_probe_start = 38
  instance.last_failed_probe_end = 41
  clock = [100.0]
  instance.started = 50
  requests = []
  def timeout_ssh(command, **kwargs):
    requests.append(command)
    if command == 'systemctl reboot':
      return subprocess.CompletedProcess([], 0, '', '')
    if requests.count('cat /proc/sys/kernel/random/boot_id') == 1:
      return subprocess.CompletedProcess([], 0, before, '')
    clock[0] += 1
    return subprocess.CompletedProcess([], 255, 'x' * 20000, 'Connection timed out during banner exchange')
  diagnostic_calls = []
  instance.qmp_socket = SimpleNamespace(settimeout=lambda value: diagnostic_calls.append(('socket-timeout', value)))
  def timeout_monitor(command, arguments=None):
    if command == 'query-status':
      return {'status': 'running'}
    saved = json.loads((directory / 'manifest.json').read_text())
    saved_proof = json.loads((directory / 'standalone-reboot.json').read_text())
    assert saved['status'] == 'standalone-reboot-failed' and not saved['validation_passed']
    assert not saved_proof['passed'] and 'within 2s' in saved_proof['failure']
    diagnostic_calls.append((command, arguments))
    if command == 'query-cpus-fast':
      raise ConnectionError('diagnostic socket closed')
    return 'retained network state'
  def timeout_screen(name):
    (directory / name).write_bytes(b'fresh standalone screen')
    return name
  instance.qmp = timeout_monitor
  instance.screenshot = timeout_screen
  instance.ssh = timeout_ssh
  instance.remove_standalone_media = lambda _: {'query_block': []}
  with patch.object(module.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(module.time, 'sleep'):
    try:
      instance.verify_standalone_reboot(roots, identity)
    except RuntimeError as error:
      assert str(error) == 'Standalone reboot did not become ready within 2s'
    else:
      raise AssertionError('standalone timeout was accepted')
  probe = json.loads((directory / 'standalone-last-failed-ssh-probe.json').read_text())
  assert probe['returncode'] == 255 and len(probe['stdout']) == 16384
  assert probe['stderr'] == 'Connection timed out during banner exchange'
  assert probe['finished_host_wall_s'] >= probe['started_host_wall_s']
  diagnostics = json.loads((directory / 'standalone-timeout-diagnostics.json').read_text())
  assert diagnostics['after_measurement_failure'] and len(diagnostics['steps']) == 9
  assert [step['label'] for step in diagnostics['steps'][:2]] == ['usernet', 'network']
  assert diagnostics['steps'][-1]['label'] == 'registers'
  assert any(step.get('error') == 'diagnostic socket closed' for step in diagnostics['steps'])
  assert diagnostic_calls[0] == ('socket-timeout', 2)
  for stage in ('before-keys', 'after-escape', 'after-tty2'):
    assert (directory / f'standalone-timeout-{stage}.png').read_bytes() == b'fresh standalone screen'
  for name, data in original.items():
    assert (directory / name).read_bytes() == data, 'standalone diagnostics overwrote initial evidence'
  assert instance.last_failed_probe_start == 38 and instance.last_failed_probe_end == 41
  assert instance.manifest['first_installed_ssh_wall_s'] == 42.125 and not instance.collected
  assert not instance.manifest['standalone_reboot_passed']

  # subprocess timeouts can contain bytes, or no partial output at all.
  instance.record_standalone_probe(subprocess.TimeoutExpired(['ssh'], 8, output=b'partial', stderr=None), 70, 78)
  probe = json.loads((directory / 'standalone-last-failed-ssh-probe.json').read_text())
  assert probe['returncode'] is None and probe['stdout'] == 'partial' and probe['stderr'] == ''
  assert probe['error'] == 'SSH command timed out after 8s'

  toolchain = os.environ.get('OMARCHY_QEMU_TOOLCHAIN')
  if toolchain:
    prefix = Path(toolchain)
    env = dict(os.environ)
    env['LD_LIBRARY_PATH'] = str(prefix / 'usr/lib/x86_64-linux-gnu')
    env['QEMU_MODULE_DIR'] = str(prefix / 'usr/lib/x86_64-linux-gnu/qemu')
    for name in ('iso.raw', 'extra.raw', 'cidata.raw'):
      with (directory / name).open('wb') as stream:
        stream.truncate(1024 * 1024)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    listener.close()
    argv = [str(prefix / 'usr/bin/qemu-system-x86_64'), '-L', str(prefix / 'usr/share/qemu'),
            '-bios', str(prefix / 'usr/share/seabios/bios-256k.bin'), '-machine', 'q35,accel=tcg',
            '-m', '64', '-nodefaults', '-display', 'none', '-S', '-device', 'qemu-xhci',
            '-qmp', f'tcp:127.0.0.1:{port},server=on,wait=off',
            '-drive', f'file={directory / "iso.raw"},format=raw,media=cdrom,if=none,id=iso',
            '-device', 'ide-cd,drive=iso,id=installer-cd',
            '-drive', f'file={directory / "extra.raw"},format=raw,media=cdrom,if=none,id=rootimage',
            '-device', 'ide-cd,drive=rootimage,id=rootimage-cd,bus=ide.1',
            '-drive', f'file={directory / "cidata.raw"},format=raw,if=none,id=cidata',
            '-device', 'usb-storage,drive=cidata,id=cidata-usb']
    with (directory / 'qemu-test.log').open('w') as log:
      child = subprocess.Popen(argv, env=env, stdout=log, stderr=log)
      instance = supervisor(directory)
      instance.args.qmp_port = port
      instance.vm = child
      try:
        for attempt in range(50):
          if child.poll() is not None:
            raise AssertionError((directory / 'qemu-test.log').read_text())
          try:
            instance.connect_qmp()
            break
          except ConnectionRefusedError:
            time.sleep(0.1)
        else:
          raise AssertionError('real QMP did not start')
        proof = instance.remove_standalone_media(plan)
        assert proof['device_deleted_event']['data']['device'] == 'cidata-usb'
        assert instance.assert_standalone_media_absent(plan) == proof['query_block']
        print('ok - actual QEMU ejects both CD-ROMs and completes CIDATA USB removal')
      finally:
        child.terminate()
        child.wait(timeout=10)
  else:
    print('note - set OMARCHY_QEMU_TOOLCHAIN to also exercise actual QMP device removal')
print('ok - standalone proof preserves install timing and rejects missing downtime, media, process or identity checks')
PY
