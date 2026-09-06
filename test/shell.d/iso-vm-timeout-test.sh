#!/bin/bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/base-test.sh"
python3 - "$ROOT" <<'PY'
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('iso_vm_timeout', Path(sys.argv[1]) / 'test/benchmarks/iso-vm.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
with tempfile.TemporaryDirectory(prefix='omarchy-timeout-') as temporary:
  supervisor = object.__new__(module.Supervisor)
  supervisor.directory = Path(temporary)
  supervisor.args = SimpleNamespace(timeout=1800, installed_boot_timeout=300)
  supervisor.manifest = {'status': 'running', 'installed_boot_restart_host_wall_s': 164.0}
  supervisor.started = time.monotonic() - 465
  supervisor.collected = False
  assert supervisor.timeout_reason(463.9) is None
  assert '300s after disk boot' in supervisor.timeout_reason(464)
  supervisor.collected = True
  assert supervisor.timeout_reason(1900) is None
  supervisor.collected = False
  supervisor.args.installed_boot_timeout = None
  assert supervisor.timeout_reason(464) is None
  assert '1800s' in supervisor.timeout_reason(1800)
  supervisor.args.installed_boot_timeout = 300
  del supervisor.manifest['installed_boot_restart_host_wall_s']
  assert supervisor.timeout_reason(1700) is None, 'live installer must not use installed deadline'

  supervisor.record_failed_probe(subprocess.CompletedProcess([], 255, '', 'Connection timed out'), 300, 303)
  probe = json.loads((supervisor.directory / 'last-failed-ssh-probe.json').read_text())
  assert probe == {'started_host_wall_s': 300, 'finished_host_wall_s': 303, 'returncode': 255, 'stderr': 'Connection timed out'}
  assert supervisor.last_failed_probe_start == 300 and supervisor.last_failed_probe_end == 303
  calls = []
  supervisor.qmp_socket = SimpleNamespace(settimeout=lambda value: calls.append(('socket-timeout', value)))
  def monitor(command, arguments=None):
    saved = json.loads((supervisor.directory / 'manifest.json').read_text())
    assert saved['status'] == 'timeout' and not saved['validation_passed'], 'guest input occurred before failure was sealed'
    calls.append((command, arguments))
    if command == 'query-cpus-fast':
      raise ConnectionError('diagnostic monitor lost')
    return 'diagnostic result'
  def screenshot(name):
    calls.append(('screenshot', name))
    return name
  supervisor.qmp = monitor
  supervisor.screenshot = screenshot
  with patch.object(module.time, 'sleep'):
    try:
      supervisor.fail_timeout('SSH readiness deadline exceeded')
    except RuntimeError as error:
      assert 'failed measurement' in str(error)
    else:
      raise AssertionError('timeout diagnostics accepted failed guest')
  result = json.loads((supervisor.directory / 'timeout-diagnostics.json').read_text())
  assert result['after_measurement_failure'] and len(result['steps']) == 10
  assert [step['label'] for step in result['steps'][:2]] == ['usernet', 'network']
  assert any(step.get('error') == 'diagnostic monitor lost' for step in result['steps'])
  assert result['steps'][-2]['label'] == 'registers', 'one failed diagnostic must not erase later evidence'
  assert result['steps'][-1]['label'] == 'live-console', 'live console must follow fresh screens and QMP state'
  assert calls[0] == ('socket-timeout', 2)
  keys = [arguments['keys'] for command, arguments in calls if command == 'send-key']
  assert keys == [[{'type':'qcode','data':'esc'}], [{'type':'qcode','data':key} for key in ('ctrl','alt','f2')]]
  assert not supervisor.collected
print('ok - installed readiness deadline preserves failed probe and invalidates before console diagnostics')
PY
