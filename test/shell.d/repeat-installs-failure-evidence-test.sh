#!/bin/bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/base-test.sh"
python3 - "$ROOT" <<'PY'
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

repo = Path(sys.argv[1])
script = repo / 'test/benchmarks/install-speed/repeat-installs.py'
spec = importlib.util.spec_from_file_location('repeat_install_failure', script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
fixture = repo / 'test/benchmarks/install-speed/results/kvm-attempts/33988339199/calibration'
with tempfile.TemporaryDirectory(prefix='omarchy-failed-sample-') as temporary:
  root = Path(temporary)
  state = root / 'state'
  run_root = state / 'runs'
  evidence = root / 'evidence'
  template = root / 'launch.json'
  template.write_text(json.dumps([sys.executable, str(module.RUNNER), 'run', '--iso', '/unused-test-fixture.iso']))
  launches = []
  warning = 'warning: cups: /var/log/cups/ (No such file or directory)\n'
  standalone_files = ('standalone-last-failed-ssh-probe.json', 'standalone-timeout-diagnostics.json',
    'standalone-timeout-before-keys.png', 'standalone-timeout-after-escape.png', 'standalone-timeout-after-tty2.png')
  def execute(argv, run, log, timeout):
    # This regression runs the actual series/comparison/copy logic with two
    # captured-data fixtures; no VM or synthetic performance claim is involved.
    shutil.copytree(fixture, run)
    (run / 'target.qcow2').write_bytes(b'disposable fixture sentinel')
    (run / 'id_ed25519').write_text('private test material must stay outside exported diagnostics')
    launches.append(run)
    if 'candidate' in run.name:
      validation = json.loads((run / 'validation.json').read_text())
      validation['package_files_exit_status'] = 1
      (run / 'validation.json').write_text(json.dumps(validation))
      manifest = json.loads((run / 'manifest.json').read_text())
      manifest['validation_passed'] = False
      (run / 'manifest.json').write_text(json.dumps(manifest))
      (run / 'package-files.stderr').write_text(warning)
      for name in standalone_files:
        (run / name).write_bytes(b'bounded diagnostic evidence')
    return 0
  argv = [str(script), '--control-launch', str(template), '--candidate-launch', str(template),
    '--run-root', str(run_root), '--evidence-root', str(evidence), '--vm-state-root', str(state)]
  with patch.object(sys, 'argv', argv), patch.object(module, 'execute_sample', execute):
    try:
      module.main()
    except ValueError as error:
      assert 'package file validation failed' in str(error), str(error)
    else:
      raise AssertionError('failed candidate Qk was accepted')
  assert len(launches) == 2
  control, candidate = launches
  assert not (control / 'target.qcow2').exists(), 'valid sample no longer follows ordinary reclamation'
  assert (candidate / 'target.qcow2').read_bytes() == b'disposable fixture sentinel'
  failed = evidence / 'failed-runs' / candidate.name
  record = json.loads((failed / 'failure-record.json').read_text())
  assert record['measurement_valid'] is False and record['status'] == 'failed'
  assert record['runner_exit_status'] == 0, 'successful runner process status must remain distinct from failed validation'
  assert 'package file validation failed' in record['failure']
  assert (failed / 'package-files.stderr').read_text() == warning
  assert record['files']['package-files.stderr']['sha256'] == module.digest(candidate / 'package-files.stderr')
  for name in standalone_files:
    assert (failed / name).read_bytes() == b'bounded diagnostic evidence'
    assert record['files'][name]['sha256'] == module.digest(candidate / name)
  assert not (failed / 'id_ed25519').exists() and not (failed / 'target.qcow2').exists()
  assert not (failed / 'seal.json').exists()
  assert not (evidence / 'runs' / candidate.name).exists()
  series = json.loads((evidence / 'series.json').read_text())
  assert series['status'] == 'failed' and len(series['runs']) == 1
  assert series['runs'][0]['revision'] == 'control'
  assert series['failed_run_evidence'] == str(failed)
  assert not (evidence / 'comparison.json').exists()
print('ok - actual Qk rejection exports bounded invalid evidence and preserves failed disk without sealing')
PY
