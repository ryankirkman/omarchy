#!/bin/bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/base-test.sh"
python3 - "$ROOT" <<'PY'
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

repo = Path(sys.argv[1])


def load(name, path):
  spec = importlib.util.spec_from_file_location(name, repo / path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


vm = load('iso_vm_install_events', 'test/benchmarks/iso-vm.py')
repeat = load('repeat_install_events', 'test/benchmarks/install-speed/repeat-installs.py')
comparison = repeat.load_comparator()
filename = 'install-log-events.json'
prefix = '/usr/share/omarchy/install/'
timestamp = '2026-09-06 12:34:56'
script_path = prefix + 'config/example-file_v2.sh'


def line(event='Starting', path=script_path, stamp=timestamp, suffix=''):
  return f'[{stamp}] {event}: {path}{suffix}'


def metadata(result, *, status='available', returncode=0, truncated=False):
  expected = {
    'schema_version': 1, 'advisory_only': True,
    'source': '/var/log/omarchy-install.log', 'source_read_exit_status': returncode,
    'clock': 'guest-wall-clock', 'resolution_seconds': 1,
    'timezone': 'unspecified-guest-local-time', 'used_for_timing_acceptance': False,
    'event_limit': 512, 'truncated': truncated, 'status': status,
  }
  assert {key: value for key, value in result.items() if key != 'events'} == expected, result


def rejects(function, message):
  try:
    function()
  except ValueError as error:
    assert message in str(error), str(error)
  else:
    raise AssertionError('Accepted invalid evidence: ' + message)


# Preserve event order and literal guest times, including a backward clock
# step. No pairing, duration inference, or success claim belongs in this file.
valid_lines = [
  line(),
  line('Completed', stamp='2026-09-06 12:34:55'),
  line('Starting', path=prefix + 'hardware/gpu.driver-2.sh', stamp='2024-02-29 00:00:00'),
  line('Failed', suffix=' (exit code: 1)'),
  line('Failed', suffix=' (exit code: 255)'),
  line(),
]
expected_events = [
  {'timestamp_text': timestamp, 'event': 'Starting', 'path': script_path},
  {'timestamp_text': '2026-09-06 12:34:55', 'event': 'Completed', 'path': script_path},
  {'timestamp_text': '2024-02-29 00:00:00', 'event': 'Starting', 'path': prefix + 'hardware/gpu.driver-2.sh'},
  {'timestamp_text': timestamp, 'event': 'Failed', 'path': script_path, 'exit_code': 1},
  {'timestamp_text': timestamp, 'event': 'Failed', 'path': script_path, 'exit_code': 255},
  {'timestamp_text': timestamp, 'event': 'Starting', 'path': script_path},
]
valid_log = '\n'.join(valid_lines) + '\n'
parsed = vm.parse_install_log_events(valid_log)
metadata(parsed)
assert parsed['events'] == expected_events
assert vm.parse_install_log_events('\r\n'.join(valid_lines))['events'] == expected_events

private = 'CONTRACT_PRIVATE_PASSWORD_82f9'
invalid = [
  private, '[INFO] ' + line(), ' ' + line(), line() + ' ',
  line() + ' ' + private, line() + '; ' + private,
  '\x1b[32m' + line(), line() + '\x1b[0m', line() + '\x00',
  line().replace('Starting:', 'Starting:\t'),
  line().replace('Starting', 'starting'), line('Finished'), line('Failed'),
  line('Starting', suffix=' (exit code: 1)'), line('Completed', suffix=' (exit code: 1)'),
]
for code in ('0', '256', '999', '1000', '-1', '+1', '01', '1.0', '1 '):
  invalid.append(line('Failed', suffix=f' (exit code: {code})'))
for bad_path in (
  '/home/person/private.sh', prefix + '../private.sh', prefix + 'config/../private.sh',
  prefix + './private.sh', prefix + 'config//private.sh', prefix + 'config/./private.sh',
  prefix + 'config/private.sh/extra', prefix + 'config/private.sh.bak',
  prefix + 'config/private', prefix + '.hidden/private.sh', prefix + 'config/秘密.sh',
  prefix + 'config/private name.sh', prefix + 'config/private\tname.sh',
  prefix + 'config/private\rname.sh', prefix + 'config/private\x00name.sh',
  prefix + 'config/private\x1fname.sh', prefix + 'config/private\x7fname.sh',
  prefix + 'config/private\u2028name.sh', prefix + 'config/private\u2029name.sh',
  prefix + 'config/$(private).sh', prefix + 'config/`private`.sh',
  prefix + 'config/private;command.sh', prefix + 'config/private\\name.sh',
  prefix + 'config/%2e%2e/private.sh', prefix + '/private.sh',
):
  invalid.append(line(path=bad_path))
for bad_stamp in (
  '2026-02-29 12:34:56', '2026-02-30 12:34:56', '2026-13-01 12:34:56',
  '2026-00-01 12:34:56', '2026-09-00 12:34:56', '2026-09-06 24:00:00',
  '2026-09-06 12:60:00', '2026-09-06 12:34:60', '0000-09-06 12:34:56',
  '2026-9-06 12:34:56', '2026-09-6 12:34:56', '2026-09-06T12:34:56',
  '2026-09-06 12:34:56Z', '2026-09-06 12:34:56.123', '２０２６-09-06 12:34:56',
):
  invalid.append(line(stamp=bad_stamp))
for rejected in invalid:
  result = vm.parse_install_log_events(rejected)
  metadata(result, status='no-recognized-events')
  assert result['events'] == [], repr(rejected)
mixed = vm.parse_install_log_events('\n'.join(invalid + valid_lines + invalid))
assert mixed == parsed, 'Unrecognized messages entered the advisory artifact'
assert private not in json.dumps(mixed)
for returncode in (1, 255):
  result = vm.parse_install_log_events(valid_log + private, returncode)
  metadata(result, status='unavailable', returncode=returncode)
  assert result['events'] == [], 'Partial stdout from an unsuccessful read was accepted'
metadata(vm.parse_install_log_events(''), status='no-recognized-events')
print('ok - log events retain only strict script records and explicitly preserve advisory wall-clock semantics')

# Invalid records do not consume the limit or claim truncation. The 513th
# recognized event must set truncation without exporting it or its successors.
at_limit = '\n'.join(line(path=prefix + f'config/event-{i:03}.sh') for i in range(512))
result = vm.parse_install_log_events(at_limit + '\n' + '\n'.join(invalid))
metadata(result)
assert len(result['events']) == 512
assert result['events'][0]['path'].endswith('event-000.sh')
assert result['events'][-1]['path'].endswith('event-511.sh')
overflow = vm.parse_install_log_events(at_limit + '\n' + '\n'.join(invalid) + '\n' + line())
metadata(overflow, truncated=True)
assert overflow['events'] == result['events']
path_512 = prefix + 'a' * 240 + '/' + 'b' * (512 - len(prefix) - 240 - 1 - 3) + '.sh'
assert len(path_512) == 512
assert vm.parse_install_log_events(line(path=path_512))['events'][0]['path'] == path_512
path_513 = path_512[:-3] + 'b.sh'
assert len(path_513) == 513
metadata(vm.parse_install_log_events(line(path=path_513)), status='no-recognized-events')
metadata(vm.parse_install_log_events(line() + private * 10000), status='no-recognized-events')
print('ok - advisory records enforce recognized-event and path bounds without false truncation')

fixture = repo / 'test/benchmarks/install-speed/results/kvm-attempts/33988339199/calibration'
timing_bytes = (fixture / 'install-timing.json').read_bytes()
roots = {'filesystems': [{'source': '/dev/vda2[/@]', 'fstype': 'btrfs', 'target': '/'}]}
expected_requests = [
  ('findmnt --json -o SOURCE,FSTYPE,TARGET /', {}),
  ('cat /var/log/omarchy-install-timing.json', {'sudo': True}),
  ('LC_ALL=C pacman -Q | LC_ALL=C sort', {'sudo': False, 'timeout': 180}),
  ('LC_ALL=C pacman -Qqe | LC_ALL=C sort', {'sudo': False, 'timeout': 180}),
  ('findmnt /boot; ls -l /boot; cat /proc/cmdline; uname -a', {'sudo': True, 'timeout': 180}),
  ('systemd-analyze --no-pager blame', {'sudo': False, 'timeout': 180}),
  ('systemd-analyze --no-pager time; systemd-analyze --no-pager critical-chain', {'sudo': False, 'timeout': 180}),
  ('cat /var/log/omarchy-install.log', {'sudo': True, 'timeout': 180}),
  ('journalctl -b --no-pager', {'sudo': True, 'timeout': 180}),
  ('LC_ALL=C pacman -Qk', {'sudo': True, 'timeout': 1200}),
]

with tempfile.TemporaryDirectory(prefix='omarchy-install-events-') as temporary:
  root = Path(temporary)
  for index, (log_stdout, log_status, package_status) in enumerate((
    (valid_log + private, 0, 0), (valid_log + private, 1, 0),
    (private, 0, 0), (valid_log, 0, 1),
  )):
    directory = root / f'collect-{index}'
    directory.mkdir()
    instance = object.__new__(vm.Supervisor)
    instance.directory = directory
    instance.args = SimpleNamespace(verify_standalone_reboot=True)
    instance.manifest = {'status': 'running', 'first_installed_ssh_wall_s': 42.125,
                         'last_failed_installed_ssh_probe_started_wall_s': 40.0}
    instance.collected = False
    requests = []
    standalone = []
    def ssh(command, **kwargs):
      requests.append((command, kwargs))
      if command == expected_requests[0][0]:
        return subprocess.CompletedProcess([], 0, json.dumps(roots), '')
      if command == expected_requests[1][0]:
        return subprocess.CompletedProcess([], 0, timing_bytes.decode(), '')
      if command == 'cat /var/log/omarchy-install.log':
        return subprocess.CompletedProcess([], log_status, log_stdout, 'private source diagnostic ' + private)
      if command == 'LC_ALL=C pacman -Qk':
        return subprocess.CompletedProcess([], package_status, 'fixture: 1 total files, 0 missing files\n', '')
      return subprocess.CompletedProcess([], 0, 'local fixture output\n', '')
    instance.ssh = ssh
    instance.collect_identity = lambda: {'fixture': 'public identifiers'}
    instance.verify_standalone_reboot = lambda *args: standalone.append(args)
    instance.screenshot = lambda name: None
    if package_status:
      try:
        instance.collect()
      except RuntimeError as error:
        assert 'complete successful phases and package validation' in str(error)
      else:
        raise AssertionError('Advisory success records bypassed package validation')
      assert not instance.collected and not standalone
    else:
      instance.collect()
      assert instance.collected and instance.manifest['validation_passed'] is True
      assert len(standalone) == 1, 'Missing/unrecognized logs changed the standalone gate'
    assert requests == expected_requests, 'Collection issued a new or changed SSH command'
    assert (directory / 'install-timing.json').read_bytes() == timing_bytes
    assert instance.manifest['first_installed_ssh_wall_s'] == 42.125
    assert instance.manifest['last_failed_installed_ssh_probe_started_wall_s'] == 40.0
    assert (directory / 'install.log').read_text() == log_stdout, 'Raw scratch evidence was rewritten'
    result = json.loads((directory / filename).read_text())
    assert result == vm.parse_install_log_events(log_stdout, log_status)
    assert private not in json.dumps(result), 'Raw stdout/stderr leaked into exported advisory records'
  print('ok - actual collection reuses its existing log read and preserves timing bytes and validation gates')

  # Use captured data for the actual retention/sealing paths. No VM runs and
  # no invented performance sample or comparison are created by this test.
  source = root / 'source'
  shutil.copytree(fixture, source)
  before = comparison.read_run(source, allow_unsealed=True)
  advisory = json.dumps(parsed, indent=2).encode() + b'\n'
  (source / filename).write_bytes(advisory)
  (source / 'install.log').write_text(private)
  (source / 'install.log.stderr').write_text(private)
  (source / 'id_ed25519').write_text(private)
  (source / 'target.qcow2').write_bytes(b'fixture disk sentinel')
  assert comparison.read_run(source, allow_unsealed=True) == before, 'Advisory events changed accepted timing or validation'
  assert filename in repeat.EVIDENCE_FILES and filename in repeat.FAILED_EVIDENCE_FILES
  destination = root / 'sealed'
  seal = repeat.seal_run(source, destination, comparison, {})
  assert (destination / filename).read_bytes() == advisory
  assert seal['files'][filename] == {'sha256': hashlib.sha256(advisory).hexdigest(), 'bytes': len(advisory)}
  after = comparison.read_run(destination)
  assert {key: value for key, value in after.items() if key != 'directory'} == {
    key: value for key, value in before.items() if key != 'directory'
  }
  assert (destination / 'install-timing.json').read_bytes() == timing_bytes
  for excluded in ('install.log', 'install.log.stderr', 'id_ed25519', 'target.qcow2'):
    assert not (destination / excluded).exists(), f'Private/raw file exported: {excluded}'
  sealed_advisory = destination / filename
  sealed_advisory.chmod(0o600)
  sealed_advisory.write_bytes(advisory + b'\n')
  rejects(lambda: comparison.read_run(destination), 'sealed evidence changed: ' + filename)

  validation = json.loads((source / 'validation.json').read_text())
  validation['package_files_exit_status'] = 1
  (source / 'validation.json').write_text(json.dumps(validation))
  rejects(lambda: comparison.read_run(source, allow_unsealed=True), 'package file validation failed')
  failed = root / 'failed'
  record = repeat.retain_failed_run(source, failed, 'fixture package validation failure', 0, {})
  assert record['measurement_valid'] is False and record['status'] == 'failed'
  assert record['runner_exit_status'] == 0
  assert (failed / filename).read_bytes() == advisory
  assert record['files'][filename] == seal['files'][filename]
  assert not (failed / 'seal.json').exists()
  for excluded in ('install.log', 'install.log.stderr', 'id_ed25519', 'target.qcow2'):
    assert not (failed / excluded).exists(), f'Private/raw failure file exported: {excluded}'
  assert (source / 'target.qcow2').read_bytes() == b'fixture disk sentinel'
  print('ok - actual success and failure retention preserve bounded events, exclude raw logs, and seal exact bytes')
PY
