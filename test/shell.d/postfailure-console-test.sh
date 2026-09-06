#!/bin/bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/base-test.sh"
python3 - "$ROOT" <<'PY'
import importlib.util
import json
from pathlib import Path
import re
import socket
import sys
import tempfile
import threading
from types import SimpleNamespace

root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('postfailure_console', root / 'test/benchmarks/postfailure-console.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

with tempfile.TemporaryDirectory(prefix='omarchy-console-contract-') as temporary:
  directory = Path(temporary)
  serial = directory / 'serial.log'
  serial.write_bytes(b'original serial evidence\n')
  manifest = directory / 'manifest.json'
  manifest.write_text(json.dumps({'status': 'installed-and-booted', 'validation_passed': True}))
  def forbidden(*args):
    raise AssertionError('console touched before failure was persisted')
  try:
    module.capture(directory, 'omarchy-benchmark', forbidden)
  except RuntimeError as error:
    assert 'persisted failed measurement' in str(error)
  else:
    raise AssertionError('valid measurement entered console diagnostics')
  assert not (directory / 'timeout-console.json').exists()

  def exercise(mode, prefix=''):
    sample = directory / mode
    sample.mkdir()
    (sample / 'serial.log').write_bytes(serial.read_bytes())
    original_manifest = {'status': 'timeout' if not prefix else 'standalone-reboot-failed',
      'validation_passed': False, 'failure': 'original timeout', 'first_installed_ssh_wall_s': 42.25,
      'qemu_argv': ['qemu-system-x86_64', '-serial', 'file:' + str(sample / 'serial.log')]}
    (sample / 'manifest.json').write_text(json.dumps(original_manifest))
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    listener.settimeout(2)
    port = listener.getsockname()[1]
    sent_by_helper = []
    server_errors = []
    def guest():
      try:
        with listener.accept()[0] as connection:
          connection.settimeout(2)
          pending = bytearray()
          def receive_until(end):
            while end not in pending:
              data = connection.recv(4096)
              if not data:
                return None
              pending.extend(data)
            offset = pending.index(end) + len(end)
            value = bytes(pending[:offset]); del pending[:offset]
            sent_by_helper.append(value)
            return value
          assert receive_until(b'\r') == b'\r'
          # Split a query at its chunk boundary before showing any login prompt.
          connection.sendall(b'\x1b[18')
          connection.sendall(b't')
          assert receive_until(b't') == b'\x1b[8;24;80t'
          connection.sendall(b'\r\nomarchy-benchmark login: ')
          assert receive_until(b'\r') == b'root\r'
          connection.sendall(b'root\r\nPassword: ')
          assert receive_until(b'\r') == b'omarchy\r'
          if mode == 'login-rejected':
            connection.sendall(b'\r\nLogin incorrect\r\nomarchy-benchmark login: ')
            assert receive_until(b'\r') is None
            return
          connection.sendall(b'\r\n\x1b[32m[root@omarchy-benchmark ~]# \x1b[0m')
          challenge = receive_until(b'\r')
          assert challenge is not None and b'id -u' in challenge
          marker = re.search(rb'OMARCHY_CONSOLE_[0-9a-f]{32}', challenge).group()
          # Echoing the command must not be confused with the actual UID reply.
          connection.sendall(challenge + b'\r\n' + marker + b'_UID\r\n' +
            (b'1000' if mode == 'wrong-uid' else b'0') + b'\r\n' + marker + b'_UID_END\r\n')
          if mode == 'wrong-uid':
            assert receive_until(b'\r') is None
            return
          commands = receive_until(b'\r')
          assert commands is not None and b'plymouth-read-write.service' in commands
          assert b'TimeoutStartUSec' in commands and b'wchan stack syscall' in commands
          for forbidden_command in (b'systemctl restart', b'systemctl reboot', b'/etc/shadow', b'/cmdline', b'/environ'):
            assert forbidden_command not in commands
          assert max(map(len, commands.splitlines())) <= 4000
          if mode == 'output-limit':
            try:
              connection.sendall(b'X' * (module.MAX_OUTPUT + 8192))
            except (BrokenPipeError, ConnectionResetError):
              pass
            return
          connection.sendall(b'\r\nCOMMAND waiting-jobs\r\nplymouth-read-write.service start waiting\r\n'
            b'COMMAND_EXIT 124\r\nsecret=value-must-not-survive\r\nomarchy\r\n'
            b'-----BEGIN OPENSSH PRIVATE KEY-----\r\nprivate-value\r\n-----END OPENSSH PRIVATE KEY-----\r\n'
            + marker + b'_DONE:0\r\n')
      except Exception as error:
        server_errors.append(error)
      finally:
        listener.close()
    worker = threading.Thread(target=guest)
    worker.start()
    def qmp(command, arguments=None):
      assert json.loads((sample / 'manifest.json').read_text()) == original_manifest
      if command == 'query-chardev':
        filename = f'disconnected:tcp:127.0.0.1:{port},server=on' if qmp.changed else 'file'
        return [{'label': 'serial0', 'filename': filename, 'frontend-open': True}]
      assert command == 'chardev-change' and arguments['id'] == 'serial0'
      addr = arguments['backend']['data']['addr']['data']
      assert addr == {'host': '127.0.0.1', 'port': '0', 'ipv4': True}
      assert arguments['backend']['data']['server'] and not arguments['backend']['data']['wait']
      qmp.changed = True
      return {}
    qmp.changed = False
    process = SimpleNamespace(args=original_manifest['qemu_argv'], pid=123, poll=lambda: None)
    result = module.capture(sample, 'omarchy-benchmark', qmp, qemu_process=process, prefix=prefix, timeout=1)
    worker.join(timeout=3)
    assert not worker.is_alive() and not server_errors, server_errors
    assert json.loads((sample / 'manifest.json').read_text()) == original_manifest
    assert (sample / 'serial.log').read_bytes() == serial.read_bytes()
    assert result['original_serial']['unchanged_during_console_capture']
    assert result['connection'] == {'host': '127.0.0.1', 'port': port}
    assert result['helper_sha256'] == module.digest(Path(module.__file__))
    assert result['observed_chardevs_before_guard'][0]['filename'] == 'file'
    assert result['owned_qemu']['actual_argv_matches_manifest']
    saved = (sample / (prefix + 'timeout-console.log')).read_text()
    status_text = (sample / (prefix + 'timeout-console.json')).read_text()
    assert 'value-must-not-survive' not in saved and 'private-value' not in saved
    assert '\nomarchy\n' not in saved and 'omarchy\\r' not in status_text
    assert not result['measurement_valid'] and result['elapsed_seconds'] < 2
    if mode == 'success':
      assert result['status'] == 'collected' and result['authenticated_root'] and result['commands_started']
      assert 'plymouth-read-write.service start waiting' in saved and 'COMMAND_EXIT 124' in saved
      assert result['suite_exit_status'] == 0 and result['maximum_command_line_bytes'] <= 4000
    else:
      assert result['status'] == 'failed'
      if mode in ('login-rejected', 'wrong-uid'):
        assert not result['commands_started'] and not result['authenticated_root']
        assert not any(b'waiting-jobs' in row for row in sent_by_helper)
      if mode == 'output-limit':
        assert '256 KiB' in result['error'] and len((sample / (prefix + 'timeout-console.log')).read_bytes()) <= module.MAX_OUTPUT
    return result
  exercise('success', 'standalone-')
  exercise('login-rejected')
  exercise('wrong-uid')
  exercise('output-limit')

  bad = directory / 'unknown-backend'; bad.mkdir()
  (bad / 'serial.log').write_bytes(b'original')
  (bad / 'manifest.json').write_text(json.dumps({'status': 'timeout', 'validation_passed': False, 'failure': 'timeout'}))
  calls = []
  result = module.capture(bad, 'omarchy-benchmark', lambda command, *args: calls.append(command) or [])
  assert result['status'] == 'failed' and calls == ['query-chardev']
  assert not result['commands_started'] and (bad / 'serial.log').read_bytes() == b'original'
  assert result['observed_chardevs_before_guard'] == []

  for mode in ('missing-process', 'exited-process', 'argv-mismatch', 'wrong-serial', 'duplicate-serial', 'unknown-kind', 'serial-symlink'):
    sample = directory / mode; sample.mkdir()
    serial_path = sample / 'serial.log'; serial_path.write_bytes(b'original')
    args = ['qemu-system-x86_64', '-serial', 'file:' + str(serial_path)]
    if mode == 'wrong-serial': args[-1] = 'file:/tmp/other.log'
    if mode == 'duplicate-serial': args += ['-serial', 'null']
    record = {'status':'timeout', 'validation_passed':False, 'failure':'timeout', 'qemu_argv':args.copy()}
    (sample / 'manifest.json').write_text(json.dumps(record))
    if mode == 'argv-mismatch': args += ['-S']
    if mode == 'serial-symlink': serial_path.unlink(); serial_path.symlink_to(serial)
    process = None if mode == 'missing-process' else SimpleNamespace(args=args, pid=123,
      poll=lambda: 1 if mode == 'exited-process' else None)
    calls = []
    rows = [{'label':'serial0','filename':'pty' if mode == 'unknown-kind' else 'file','frontend-open':True}]
    result = module.capture(sample, 'omarchy-benchmark', lambda command, *a: calls.append(command) or rows,
      qemu_process=process)
    assert result['status'] == 'failed' and calls == ['query-chardev'], mode
    assert result['observed_chardevs_before_guard'] == rows and not result['interventions'], mode
    assert not result['commands_started'] and not result['authenticated_root'], mode
print('ok - post-failure console enforces login/UID boundaries, redaction, output/deadline limits and unchanged evidence')
PY
